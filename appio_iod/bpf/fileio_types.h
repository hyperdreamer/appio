#ifndef APPIO_FILEIO_TYPES_H
#define APPIO_FILEIO_TYPES_H

#include <linux/types.h>

#ifndef __preserve_access_index
#define __preserve_access_index __attribute__((preserve_access_index))
#endif

/* Kernel typedef spellings used by the six VFS prototypes. */
typedef unsigned long size_t;
typedef long ssize_t;
typedef long long loff_t;
typedef unsigned int rwf_t;
typedef __u16 umode_t;

struct iovec;
struct iov_iter;

struct block_device {
    __u32 bd_dev;
} __preserve_access_index;

struct super_block {
    struct block_device *s_bdev;
} __preserve_access_index;

struct qstr {
    const unsigned char *name;
    __u32 len;
} __preserve_access_index;

struct dentry {
    struct qstr d_name;
} __preserve_access_index;

struct path {
    struct dentry *dentry;
} __preserve_access_index;

struct inode {
    umode_t i_mode;
    __u64 i_ino;
    __u32 i_generation;
    struct super_block *i_sb;
} __preserve_access_index;

struct file {
    struct inode *f_inode;
    struct path f_path;
} __preserve_access_index;

#endif
