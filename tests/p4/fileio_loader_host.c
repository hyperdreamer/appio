/* Execute the production drain, recording the kernel-helper boundary ordering. */
#define _GNU_SOURCE
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static int fake_next(int fd, const void *key, void *next);
static int fake_lookup(int fd, const void *key, void *value);
static int fake_delete(int fd, const void *key);
static int fake_update(int fd, const void *key, const void *value, __u64 flags);
static ssize_t fake_write(int fd, const void *data, size_t size);
static int fake_clock(clockid_t clock_id, struct timespec *value);
static int fake_sleep(const struct timespec *delay, struct timespec *remaining);
#define bpf_map_get_next_key fake_next
#define bpf_map_lookup_elem fake_lookup
#define bpf_map_delete_elem fake_delete
#define bpf_map_update_elem fake_update
#define write fake_write
#define clock_gettime fake_clock
#define nanosleep fake_sleep
#define main fileio_loader_main
#include "fileio_loader.c"
#undef main
#undef write
#undef clock_gettime
#undef nanosleep

static unsigned int active_slot;
static __u64 raw_device = 0x10300005;
/* event slots 0/1 at fd 20/21; stats slots 0/1 at fd 22/23. */
static unsigned char live[4][55];
static unsigned char output[32768];
static size_t output_size;
static char order[32];
static size_t order_size;
static unsigned int busy_reads;
static unsigned int wait_reads;
static unsigned int sleep_calls;
static int quiescent;
static int event_clear_started;
static int stats_clear_started;
static int cleared_while_busy;
static int wait_mode;
static long clock_ticks;

static void record(char event)
{
    if (order_size + 1 >= sizeof(order)) abort();
    order[order_size++] = event;
    order[order_size] = '\0';
}

static int absent(void) { errno = ENOENT; return -ENOENT; }
static int index_for(int fd) { if (fd < 20 || fd > 23) abort(); return fd - 20; }
static __attribute__((noinline)) unsigned int key_index(int fd, const void *key)
{
    return fd < 22 ? (unsigned int)((const struct fileio_key *)key)->inode - 1
                   : (unsigned int)*(const __u64 *)key - 1;
}

static int fake_next(int fd, const void *key, void *next)
{
    int map = index_for(fd);
    if (fd < 22 && !event_clear_started) {
        record('E');
        event_clear_started = 1;
    }
    if (fd >= 22 && !stats_clear_started) {
        record('T');
        stats_clear_started = 1;
    }
    if (!quiescent) cleared_while_busy = 1;
    unsigned int start = key ? key_index(fd, key) + 1 : 0;
    for (unsigned int i = start; i < 55; i++) {
        if (!live[map][i]) continue;
        if (fd < 22) {
            struct fileio_key event = {.cgroup_id = 42, .device = raw_device,
                                       .inode = i + 1, .i_generation = 7};
            memcpy(next, &event, sizeof(event));
        } else {
            __u64 cgroup = i + 1;
            memcpy(next, &cgroup, sizeof(cgroup));
        }
        return 0;
    }
    return absent();
}

static int fake_lookup(int fd, const void *key, void *value)
{
    if (fd == 10) {
        if (*(__u32 const *)key != 0) abort();
        memcpy(value, &active_slot, sizeof(active_slot));
        return 0;
    }
    if (fd == 11) {
        if (*(__u32 const *)key != 0) abort();
        wait_reads++;
        if (wait_mode == 2) { record('X'); errno = EIO; return -EIO; }
        __u64 in_flight = wait_mode == 1 || busy_reads != 0;
        if (busy_reads != 0) busy_reads--;
        if (in_flight) record('B');
        else { record('Q'); quiescent = 1; }
        memcpy(value, &in_flight, sizeof(in_flight));
        return 0;
    }
    unsigned int i = key_index(fd, key);
    if (i >= 55 || !live[index_for(fd)][i]) return absent();
    if (fd < 22) {
        struct fileio_value event = {.reads = 1, .bytes_r = 17,
                                     .name_len = 3, .name = "log"};
        memcpy(value, &event, sizeof(event));
    } else {
        struct fileio_cgroup_stats stats = {0};
        memcpy(value, &stats, sizeof(stats));
    }
    return 0;
}

static int fake_delete(int fd, const void *key)
{
    unsigned int i = key_index(fd, key);
    if (i >= 55 || !live[index_for(fd)][i]) return absent();
    if (!quiescent) cleared_while_busy = 1;
    live[index_for(fd)][i] = 0;
    return 0;
}

static int fake_update(int fd, const void *key, const void *value, __u64 flags)
{
    if (fd != 10 || *(__u32 const *)key != 0 || flags != BPF_ANY) abort();
    if (*(__u32 const *)value != 1 - active_slot) abort();
    record('F');
    memcpy(&active_slot, value, sizeof(active_slot));
    return 0;
}

static ssize_t fake_write(int fd, const void *data, size_t size)
{
    if (fd != STDOUT_FILENO || output_size + size > sizeof(output)) abort();
    if (output_size == 0) record('A');
    memcpy(output + output_size, data, size);
    output_size += size;
    return (ssize_t)size;
}

static int fake_clock(clockid_t clock_id, struct timespec *value)
{
    if (clock_id != CLOCK_MONOTONIC) abort();
    value->tv_sec = 0;
    value->tv_nsec = clock_ticks++ * (wait_mode == 1 ? 600000000L : 1000000L);
    return 0;
}

static int fake_sleep(const struct timespec *delay, struct timespec *remaining)
{
    if (delay->tv_sec != 0 || delay->tv_nsec != 1000000L || remaining != NULL) abort();
    record('S');
    sleep_calls++;
    return 0;
}

static unsigned int count(int map)
{
    unsigned int result = 0;
    for (unsigned int i = 0; i < 55; i++) result += live[map][i] != 0;
    return result;
}

int main(int argc, char **argv)
{
    /* A missing drain call must fail behaviorally, not as an unused-function warning. */
    (void)&wait_for_quiescence;
    if (argc >= 2) raw_device = strtoull(argv[1], NULL, 0);
    if (argc == 3) {
        if (strcmp(argv[2], "timeout") == 0) wait_mode = 1;
        else if (strcmp(argv[2], "lookup-error") == 0) wait_mode = 2;
        else if (strcmp(argv[2], "success") != 0) return 2;
    }
    struct loader_state state = {.active_slot_fd = 10, .in_flight_fd = 11,
        .event_fd = {20, 21}, .stats_fd = {22, 23}, .hook_mask = 63, .ready = true};
    struct fileio_flip_request request = {.epoch = 1, .seq = 1};
    memset(live[0], 1, 55);
    memset(live[1], 1, 7);
    live[2][0] = live[3][0] = 1;
    for (int pass = 0; pass < (wait_mode ? 1 : 3); pass++) {
        output_size = order_size = 0;
        order[0] = '\0';
        busy_reads = 2;
        wait_reads = sleep_calls = 0;
        quiescent = event_clear_started = stats_clear_started = cleared_while_busy = 0;
        clock_ticks = 0;
        if (drain(&state, &request) != 0) return 1;
        if (output_size < sizeof(struct fileio_drain_response)) return 1;
        struct fileio_drain_response response;
        memcpy(&response, output, sizeof(response));
        struct fileio_file_record event = {0};
        if (response.event_count)
            memcpy(&event, output + sizeof(response), sizeof(event));
        printf("{\"slot\":%u,\"events\":%u,\"stats\":%u,\"status\":%u,"
               "\"active\":%u,\"event_counts\":[%u,%u],\"stats_counts\":[%u,%u],"
               "\"device\":[%u,%u],\"order\":\"%s\",\"cleared_while_busy\":%d,"
               "\"wait_reads\":%u,\"sleep_calls\":%u,\"error\":%d,\"complete\":%u}\n",
               response.drained_slot, response.event_count, response.stats_count,
               response.status, active_slot, count(0), count(1), count(2), count(3),
               event.major, event.minor, order, cleared_while_busy, wait_reads, sleep_calls,
               response.error, response.complete);
        request.seq++;
    }
    return 0;
}
