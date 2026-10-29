#include <linux/bpf.h>
#include <linux/stat.h>
#include <linux/types.h>

#include <bpf/bpf_core_read.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "fileio_protocol.h"
#include "fileio_types.h"

#ifndef S_ISREG
#define S_ISREG(mode) (((mode) & S_IFMT) == S_IFREG)
#endif

#define FILEIO_READ 0
#define FILEIO_WRITE 1
#define FILEIO_HOOK_VFS_READ 0
#define FILEIO_HOOK_VFS_WRITE 1
#define FILEIO_HOOK_VFS_READV 2
#define FILEIO_HOOK_VFS_WRITEV 3
#define FILEIO_HOOK_VFS_ITER_READ 4
#define FILEIO_HOOK_VFS_ITER_WRITE 5

struct fileio_key {
    __u64 cgroup_id;
    __u64 device;
    __u64 inode;
    __u32 i_generation;
    __u32 reserved;
};

struct fileio_value {
    __u64 reads;
    __u64 writes;
    __u64 bytes_r;
    __u64 bytes_w;
    __u32 name_len;
    char name[40];
};

struct fileio_event_map {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 262144);
    __type(key, struct fileio_key);
    __type(value, struct fileio_value);
};

struct fileio_event_map fileio_slot_0 SEC(".maps");
struct fileio_event_map fileio_slot_1 SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY_OF_MAPS);
    __uint(max_entries, 2);
    __type(key, __u32);
    __array(values, typeof(fileio_slot_0));
} fileio_slots SEC(".maps") = {
    .values = {
        [0] = &fileio_slot_0,
        [1] = &fileio_slot_1,
    },
};

struct fileio_cgroup_stats_value {
    __u64 cgroup_id;
    __u64 hook_hits[6];
    __u64 overflow_files;
    __u64 overflow_bytes_r;
    __u64 overflow_bytes_w;
    __u64 skipped_nonfile;
    __u64 skipped_unsupported;
};

_Static_assert(sizeof(struct fileio_cgroup_stats_value) == sizeof(struct fileio_cgroup_stats),
               "BPF stats layout must match the wire stats layout");

struct fileio_stats_map {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 65536);
    __type(key, __u64);
    __type(value, struct fileio_cgroup_stats_value);
};

struct fileio_stats_map fileio_stats_slot_0 SEC(".maps");
struct fileio_stats_map fileio_stats_slot_1 SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY_OF_MAPS);
    __uint(max_entries, 2);
    __type(key, __u32);
    __array(values, typeof(fileio_stats_slot_0));
} fileio_stats_slots SEC(".maps") = {
    .values = {
        [0] = &fileio_stats_slot_0,
        [1] = &fileio_stats_slot_1,
    },
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u32);
} fileio_active_slot SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} fileio_in_flight SEC(".maps");

static __always_inline struct fileio_cgroup_stats_value *fileio_stats_for(__u64 cgroup_id)
{
    __u32 zero = 0;
    __u32 *active = bpf_map_lookup_elem(&fileio_active_slot, &zero);
    if (active == NULL || *active > 1)
        return NULL;

    void *stats_map = bpf_map_lookup_elem(&fileio_stats_slots, active);
    if (stats_map == NULL)
        return NULL;

    struct fileio_cgroup_stats_value *stats = bpf_map_lookup_elem(stats_map, &cgroup_id);
    if (stats != NULL)
        return stats;

    struct fileio_cgroup_stats_value initial = {};
    initial.cgroup_id = cgroup_id;
    if (bpf_map_update_elem(stats_map, &cgroup_id, &initial, BPF_ANY) != 0)
        return NULL;
    return bpf_map_lookup_elem(stats_map, &cgroup_id);
}

static __always_inline void fileio_mark_skip(__u64 cgroup_id, int unsupported)
{
    struct fileio_cgroup_stats_value *stats = fileio_stats_for(cgroup_id);
    if (stats == NULL)
        return;
    if (unsupported)
        __sync_fetch_and_add(&stats->skipped_unsupported, 1);
    else
        __sync_fetch_and_add(&stats->skipped_nonfile, 1);
}

static __always_inline void fileio_copy_name(struct fileio_value *value,
                                             const unsigned char *name,
                                             __u32 length)
{
    value->name_len = length > FILEIO_NAME_CAP ? FILEIO_NAME_CAP : length;
    if (name == NULL)
        return;

    for (__u32 index = 0; index < FILEIO_NAME_CAP; index++) {
        if (index < value->name_len)
            bpf_probe_read_kernel(&value->name[index], sizeof(value->name[index]), name + index);
    }
}

static __always_inline int fileio_account(struct file *file, ssize_t ret,
                                          int direction, __u32 hook)
{
    if (ret <= 0)
        return 0;

    __u64 cgroup_id = bpf_get_current_cgroup_id();
    __u32 zero = 0;
    __u64 *in_flight = bpf_map_lookup_elem(&fileio_in_flight, &zero);
    if (in_flight != NULL)
        __sync_fetch_and_add(in_flight, 1);

    struct fileio_cgroup_stats_value *stats = fileio_stats_for(cgroup_id);
    if (stats != NULL)
        __sync_fetch_and_add(&stats->hook_hits[hook], 1);

    if (file == NULL) {
        fileio_mark_skip(cgroup_id, 0);
        goto out;
    }

    struct inode *inode = BPF_CORE_READ(file, f_inode);
    if (inode == NULL) {
        fileio_mark_skip(cgroup_id, 0);
        goto out;
    }
    umode_t mode = BPF_CORE_READ(inode, i_mode);
    if (!S_ISREG(mode)) {
        fileio_mark_skip(cgroup_id, 0);
        goto out;
    }

    struct super_block *sb = BPF_CORE_READ(inode, i_sb);
    if (sb == NULL || BPF_CORE_READ(sb, s_bdev) == NULL) {
        fileio_mark_skip(cgroup_id, 1);
        goto out;
    }
    struct block_device *bdev = BPF_CORE_READ(sb, s_bdev);

    struct fileio_key key = {};
    key.cgroup_id = cgroup_id;
    /* CO-RE relocates bd_dev by name; retain its raw kernel dev_t encoding. */
    key.device = (__u64)BPF_CORE_READ(bdev, bd_dev);
    key.inode = (__u64)BPF_CORE_READ(inode, i_ino);
    key.i_generation = BPF_CORE_READ(inode, i_generation);

    struct fileio_value value = {};
    struct dentry *dentry = BPF_CORE_READ(file, f_path.dentry);
    if (dentry != NULL) {
        const unsigned char *name = BPF_CORE_READ(dentry, d_name.name);
        __u32 name_length = BPF_CORE_READ(dentry, d_name.len);
        fileio_copy_name(&value, name, name_length);
    }
    if (direction == FILEIO_WRITE) {
        value.writes = 1;
        value.bytes_w = (__u64)ret;
    } else {
        value.reads = 1;
        value.bytes_r = (__u64)ret;
    }

    __u32 *active = bpf_map_lookup_elem(&fileio_active_slot, &zero);
    if (active == NULL || *active > 1)
        goto out;
    void *event_map = bpf_map_lookup_elem(&fileio_slots, active);
    if (event_map == NULL)
        goto out;

    struct fileio_value *current = bpf_map_lookup_elem(event_map, &key);
    if (current != NULL) {
        __sync_fetch_and_add(&current->reads, value.reads);
        __sync_fetch_and_add(&current->writes, value.writes);
        __sync_fetch_and_add(&current->bytes_r, value.bytes_r);
        __sync_fetch_and_add(&current->bytes_w, value.bytes_w);
    } else {
        long update_rc = bpf_map_update_elem(event_map, &key, &value, BPF_ANY);
        if (update_rc != 0) {
            struct fileio_cgroup_stats_value *overflow_stats = fileio_stats_for(cgroup_id);
            if (overflow_stats != NULL) {
                __sync_fetch_and_add(&overflow_stats->overflow_files, 1);
                if (direction == FILEIO_WRITE)
                    __sync_fetch_and_add(&overflow_stats->overflow_bytes_w, (__u64)ret);
                else
                    __sync_fetch_and_add(&overflow_stats->overflow_bytes_r, (__u64)ret);
            }
        }
    }

out:
    if (in_flight != NULL)
        __sync_fetch_and_sub(in_flight, 1);
    return 0;
}

SEC("fexit/vfs_read")
int BPF_PROG(fileio_vfs_read, struct file *file, char *buf, size_t count,
             loff_t *pos, ssize_t ret)
{
    (void)ctx;
    (void)buf;
    (void)count;
    (void)pos;
    return fileio_account(file, ret, FILEIO_READ, FILEIO_HOOK_VFS_READ);
}

SEC("fexit/vfs_write")
int BPF_PROG(fileio_vfs_write, struct file *file, const char *buf, size_t count,
             loff_t *pos, ssize_t ret)
{
    (void)ctx;
    (void)buf;
    (void)count;
    (void)pos;
    return fileio_account(file, ret, FILEIO_WRITE, FILEIO_HOOK_VFS_WRITE);
}

SEC("fexit/vfs_readv")
int BPF_PROG(fileio_vfs_readv, struct file *file, const struct iovec *vec,
             unsigned long vlen, loff_t *pos, rwf_t flags, ssize_t ret)
{
    (void)ctx;
    (void)vec;
    (void)vlen;
    (void)pos;
    (void)flags;
    return fileio_account(file, ret, FILEIO_READ, FILEIO_HOOK_VFS_READV);
}

SEC("fexit/vfs_writev")
int BPF_PROG(fileio_vfs_writev, struct file *file, const struct iovec *vec,
             unsigned long vlen, loff_t *pos, rwf_t flags, ssize_t ret)
{
    (void)ctx;
    (void)vec;
    (void)vlen;
    (void)pos;
    (void)flags;
    return fileio_account(file, ret, FILEIO_WRITE, FILEIO_HOOK_VFS_WRITEV);
}

SEC("fexit/vfs_iter_read")
int BPF_PROG(fileio_vfs_iter_read, struct file *file, struct iov_iter *iter,
             loff_t *ppos, rwf_t flags, ssize_t ret)
{
    (void)ctx;
    (void)iter;
    (void)ppos;
    (void)flags;
    return fileio_account(file, ret, FILEIO_READ, FILEIO_HOOK_VFS_ITER_READ);
}

SEC("fexit/vfs_iter_write")
int BPF_PROG(fileio_vfs_iter_write, struct file *file, struct iov_iter *iter,
             loff_t *ppos, rwf_t flags, ssize_t ret)
{
    (void)ctx;
    (void)iter;
    (void)ppos;
    (void)flags;
    return fileio_account(file, ret, FILEIO_WRITE, FILEIO_HOOK_VFS_ITER_WRITE);
}

char LICENSE[] SEC("license") = "GPL";
