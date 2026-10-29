/* Rootless execution of the production helpers, with deterministic map races. */
#include <errno.h>
#include <linux/bpf.h>
#include <linux/types.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static void *host_lookup(const void *map, const void *key);
static long host_update(const void *map, const void *key, const void *value, __u64 flags);
static __u64 host_cgroup(void);
static long host_read(void *dst, __u32 size, const void *src);
static __u64 host_atomic_add(__u64 *counter, __u64 delta);
static __u64 host_atomic_sub(__u64 *counter, __u64 delta);

#define __uint(name, value) int (*name)[value]
#define __type(name, value) value *name
#define __array(name, value) value *name[]
#define SEC(name)
#define __preserve_access_index
#define BPF_CORE_READ(src, field) ((src)->field)
#define bpf_map_lookup_elem host_lookup
#define bpf_map_update_elem host_update
#define bpf_get_current_cgroup_id host_cgroup
#define bpf_probe_read_kernel host_read
#define __sync_fetch_and_add host_atomic_add
#define __sync_fetch_and_sub host_atomic_sub
/* glibc and the kernel spell loff_t differently; no helper uses this typedef. */
#define loff_t fileio_host_loff_t
#include "fileio_host_helpers.h"
#undef loff_t

static __u32 active_slot;
static __u64 in_flight;
static __u64 cgroup_id = 42;
static struct fileio_value events[2];
static struct fileio_cgroup_stats_value stats[2];
static int event_present[2];
static int stats_present[2];
static int race_event;
static int race_stats;
static int race_skip;
static int event_error;
static int stats_error;
static int direction = FILEIO_READ;
static struct block_device device = {.bd_dev = 0x10300005};
static struct super_block sb = {.s_bdev = &device};
static struct inode inode = {.i_mode = S_IFREG, .i_ino = 99, .i_generation = 7, .i_sb = &sb};
static struct dentry dentry = {.d_name = {.name = (const unsigned char *)"log", .len = 3}};
static struct file file = {.f_inode = &inode, .f_path = {.dentry = &dentry}};

static int slot_of(const void *map, int stats_map)
{
    if (stats_map) {
        if (map == &fileio_stats_slot_0) return 0;
        if (map == &fileio_stats_slot_1) return 1;
    } else {
        if (map == &fileio_slot_0) return 0;
        if (map == &fileio_slot_1) return 1;
    }
    return -1;
}

static void *host_lookup(const void *map, const void *key)
{
    if (map == &fileio_active_slot) {
        if (*(__u32 const *)key != 0) abort();
        return &active_slot;
    }
    if (map == &fileio_in_flight) {
        if (*(__u32 const *)key != 0) abort();
        return &in_flight;
    }
    if (map == &fileio_slots) {
        if (*(__u32 const *)key > 1) abort();
        return *(__u32 const *)key == 0 ? &fileio_slot_0 : &fileio_slot_1;
    }
    if (map == &fileio_stats_slots) {
        if (*(__u32 const *)key > 1) abort();
        return *(__u32 const *)key == 0 ? &fileio_stats_slot_0 : &fileio_stats_slot_1;
    }
    int slot = slot_of(map, 1);
    if (slot >= 0) {
        if (*(__u64 const *)key != cgroup_id) abort();
        return stats_present[slot] ? &stats[slot] : NULL;
    }
    slot = slot_of(map, 0);
    if (slot < 0) abort();
    const struct fileio_key *event_key = key;
    if (event_key->cgroup_id != cgroup_id || event_key->device != 0x10300005 ||
        event_key->inode != 99 || event_key->i_generation != 7 || event_key->reserved != 0)
        abort();
    return event_present[slot] ? &events[slot] : NULL;
}

static long host_update(const void *map, const void *key, const void *value, __u64 flags)
{
    if (flags != BPF_ANY && flags != BPF_NOEXIST) abort();
    int slot = slot_of(map, 1);
    if (slot >= 0) {
        if (*(__u64 const *)key != cgroup_id) abort();
        if (race_stats) {
            race_stats = 0;
            /* Another CPU wins after the caller's absent lookup, before insert. */
            if (race_skip) fileio_mark_skip(cgroup_id, 1);
            else fileio_account(&file, 70, direction, FILEIO_HOOK_VFS_READ);
        }
        if (stats_error) return stats_error;
        if (flags == BPF_NOEXIST && stats_present[slot]) return -EEXIST;
        memcpy(&stats[slot], value, sizeof(stats[slot]));
        stats_present[slot] = 1;
        return 0;
    }
    slot = slot_of(map, 0);
    if (slot < 0) abort();
    if (race_event) {
        race_event = 0;
        fileio_account(&file, 70, direction, FILEIO_HOOK_VFS_READ);
    }
    if (event_error) return event_error;
    if (flags == BPF_NOEXIST && event_present[slot]) return -EEXIST;
    memcpy(&events[slot], value, sizeof(events[slot]));
    event_present[slot] = 1;
    return 0;
}

static __u64 host_cgroup(void) { return cgroup_id; }

/* The scheduler switches callers only at map update boundaries. Each arithmetic
 * operation here is one indivisible host step with the builtin's return value. */
static __u64 host_atomic_add(__u64 *counter, __u64 delta)
{
    __u64 previous = *counter;
    *counter += delta;
    return previous;
}

static __u64 host_atomic_sub(__u64 *counter, __u64 delta)
{
    __u64 previous = *counter;
    *counter -= delta;
    return previous;
}

static long host_read(void *dst, __u32 size, const void *src)
{
    memcpy(dst, src, size);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2 || argc > 4) return 2;
    if (argc >= 3) active_slot = (__u32)strtoul(argv[2], NULL, 10);
    if (active_slot > 1) return 2;
    if (argc == 4) cgroup_id = strtoull(argv[3], NULL, 10);
    int sequence = 0;
    ssize_t returned_bytes = 30;
    if (strcmp(argv[1], "race-event") == 0) race_event = 1;
    else if (strcmp(argv[1], "race-stats") == 0) race_stats = 1;
    else if (strcmp(argv[1], "race-skip") == 0) race_stats = race_skip = 1;
    else if (strcmp(argv[1], "event-full") == 0) event_error = -E2BIG;
    else if (strcmp(argv[1], "stats-full") == 0) stats_error = -E2BIG;
    else if (strcmp(argv[1], "event-disappears") == 0) event_error = -EEXIST;
    else if (strcmp(argv[1], "stats-disappears") == 0) stats_error = -EEXIST;
    else if (strcmp(argv[1], "read-wide") == 0) returned_bytes = 4294967313L;
    else if (strcmp(argv[1], "read-sequence") == 0) sequence = 1;
    else if (strcmp(argv[1], "overflow-sequence") == 0) {
        sequence = 1;
        event_error = -E2BIG;
    } else return 2;

    if (sequence) {
        fileio_account(&file, 70, FILEIO_READ, FILEIO_HOOK_VFS_READ);
        fileio_account(&file, 30, FILEIO_READ, FILEIO_HOOK_VFS_READ);
        fileio_account(&file, 11, FILEIO_WRITE, FILEIO_HOOK_VFS_WRITE);
        fileio_account(&file, 0, FILEIO_READ, FILEIO_HOOK_VFS_READ);
        fileio_account(&file, -EIO, FILEIO_READ, FILEIO_HOOK_VFS_READ);
    } else if (race_skip) fileio_mark_skip(cgroup_id, 0);
    else fileio_account(&file, returned_bytes, direction, FILEIO_HOOK_VFS_READ);
    const struct fileio_value *event = &events[active_slot];
    const struct fileio_cgroup_stats_value *stat = &stats[active_slot];
    printf("{\"bytes_r\":%llu,\"bytes_w\":%llu,\"reads\":%llu,\"writes\":%llu,"
           "\"hook_hits\":%llu,\"hook_hits_w\":%llu,\"overflow_files\":%llu,\"overflow_bytes_r\":%llu,"
           "\"overflow_bytes_w\":%llu,\"skipped_nonfile\":%llu,"
           "\"skipped_unsupported\":%llu,\"in_flight\":%llu,"
           "\"event_present\":%d,\"stats_present\":%d,\"cgroup_id\":%llu}\n",
           (unsigned long long)event->bytes_r, (unsigned long long)event->bytes_w,
           (unsigned long long)event->reads, (unsigned long long)event->writes,
           (unsigned long long)stat->hook_hits[FILEIO_HOOK_VFS_READ],
           (unsigned long long)stat->hook_hits[FILEIO_HOOK_VFS_WRITE],
           (unsigned long long)stat->overflow_files, (unsigned long long)stat->overflow_bytes_r,
           (unsigned long long)stat->overflow_bytes_w, (unsigned long long)stat->skipped_nonfile,
           (unsigned long long)stat->skipped_unsupported, (unsigned long long)in_flight,
           event_present[active_slot], stats_present[active_slot],
           (unsigned long long)stat->cgroup_id);
    return 0;
}
