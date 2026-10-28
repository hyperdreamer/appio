#ifndef APPIO_FILEIO_PROTOCOL_H
#define APPIO_FILEIO_PROTOCOL_H

#include <linux/types.h>

#define FILEIO_ABI_VERSION 1
#define FILEIO_NAME_CAP 40
#define FILEIO_MAX_MAP_ENTRIES 262144
#define FILEIO_CGROUP_UNKNOWN 0
#define FILEIO_HOOK_MASK_ALL 0x3f
#define FILEIO_DIAGNOSTIC_CAP 512

/* These descriptors are shared with the rootless Python decoder. */
#define FILEIO_FILE_RECORD_FORMAT "<QIIQIQQQQI40s"
#define FILEIO_CGROUP_STATS_FORMAT "<QQQQQQQQQQQQ"

#define FILEIO_DRAIN_COMPLETE 1

enum fileio_opcode {
    FILEIO_OP_HELLO = 1,
    FILEIO_OP_FLIP_DRAIN = 2,
    FILEIO_OP_CLOSE = 3,
};

enum fileio_status {
    FILEIO_STATUS_OK = 0,
    FILEIO_STATUS_ERROR = 1,
};

struct fileio_command_header {
    __u16 abi_version;
    __u16 opcode;
    __u32 payload_len;
} __attribute__((packed));

struct fileio_flip_request {
    __u64 epoch;
    __u64 seq;
} __attribute__((packed));

struct fileio_hello_response {
    __u16 abi_version;
    __u16 status;
    __u32 hook_mask;
    __u32 map_capacity;
    __s32 error;
    __u32 verifier_len;
} __attribute__((packed));

struct fileio_ack_response {
    __u16 abi_version;
    __u16 status;
    __s32 error;
    __u32 diagnostic_len;
} __attribute__((packed));

struct fileio_drain_response {
    __u16 abi_version;
    __u16 status;
    __u32 hook_mask;
    __u32 map_capacity;
    __s32 error;
    __u32 drained_slot;
    __u64 epoch;
    __u64 seq;
    __u32 event_count;
    __u32 stats_count;
    __u32 complete;
    __u32 diagnostic_len;
} __attribute__((packed));

struct fileio_file_record {
    __u64 cgroup_id;
    __u32 major;
    __u32 minor;
    __u64 inode;
    __u32 generation;
    __u64 reads;
    __u64 writes;
    __u64 bytes_r;
    __u64 bytes_w;
    __u32 name_len;
    __u8 name[FILEIO_NAME_CAP];
} __attribute__((packed));

struct fileio_cgroup_stats {
    __u64 cgroup_id;
    __u64 hook_hits[6];
    __u64 overflow_files;
    __u64 overflow_bytes_r;
    __u64 overflow_bytes_w;
    __u64 skipped_nonfile;
    __u64 skipped_unsupported;
} __attribute__((packed));

#endif
