/* Inject ENOSPC only for writes exceeding the configured WAL file size.
 * Loaded solely into a disposable test subprocess with LD_PRELOAD. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/types.h>

static int deny_wal_write(int fd, off64_t offset, size_t count) {
    const char *limit = getenv("APPIO_TEST_WAL_LIMIT");
    if (!limit || !*limit) return 0;
    char linkpath[64], path[4096];
    snprintf(linkpath, sizeof(linkpath), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(linkpath, path, sizeof(path) - 1);
    if (n < 4) return 0;
    path[n] = '\0';
    if (strcmp(path + n - 4, "-wal") != 0) return 0;
    if ((unsigned long long)offset + count <= strtoull(limit, NULL, 10)) return 0;
    static int reported = 0;
    if (!reported) {
        static const char message[] = "test: WAL ENOSPC injected\n";
        (void)write(2, message, sizeof(message) - 1);
        reported = 1;
    }
    errno = ENOSPC;
    return 1;
}

ssize_t pwrite64(int fd, const void *buf, size_t count, off64_t offset) {
    static ssize_t (*next)(int, const void *, size_t, off64_t);
    if (!next) next = dlsym(RTLD_NEXT, "pwrite64");
    if (deny_wal_write(fd, offset, count)) return -1;
    return next(fd, buf, count, offset);
}

ssize_t pwrite(int fd, const void *buf, size_t count, off_t offset) {
    static ssize_t (*next)(int, const void *, size_t, off_t);
    if (!next) next = dlsym(RTLD_NEXT, "pwrite");
    if (deny_wal_write(fd, offset, count)) return -1;
    return next(fd, buf, count, offset);
}
