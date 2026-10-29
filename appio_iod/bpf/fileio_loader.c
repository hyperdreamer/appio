#define _GNU_SOURCE

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/bpf.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include "fileio_protocol.h"

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
    unsigned char name[40];
};

struct record_vector {
    struct fileio_file_record *items;
    size_t count;
    size_t capacity;
};

struct stats_vector {
    struct fileio_cgroup_stats *items;
    size_t count;
    size_t capacity;
};

struct loader_state {
    struct bpf_object *object;
    struct bpf_link *links[6];
    int events_outer_fd;
    int stats_outer_fd;
    int active_slot_fd;
    int in_flight_fd;
    int event_fd[2];
    int stats_fd[2];
    __u32 hook_mask;
    int last_errno;
    bool ready;
    bool draining;
    char diagnostic[FILEIO_DIAGNOSTIC_CAP + 1];
};

static const char *const hook_programs[6] = {
    "fileio_vfs_read",
    "fileio_vfs_write",
    "fileio_vfs_readv",
    "fileio_vfs_writev",
    "fileio_vfs_iter_read",
    "fileio_vfs_iter_write",
};

static int loader_log(enum libbpf_print_level level, const char *format, va_list args)
{
    (void)level;
    return vfprintf(stderr, format, args);
}

static void set_diagnostic(struct loader_state *state, int error, const char *format, ...)
{
    va_list args;
    state->last_errno = error < 0 ? -error : error;
    va_start(args, format);
    vsnprintf(state->diagnostic, sizeof(state->diagnostic), format, args);
    va_end(args);
    state->diagnostic[FILEIO_DIAGNOSTIC_CAP] = '\0';
}

static int read_full(int fd, void *buffer, size_t length)
{
    unsigned char *cursor = buffer;
    while (length != 0) {
        ssize_t read_count = read(fd, cursor, length);
        if (read_count == 0)
            return -EPIPE;
        if (read_count < 0) {
            if (errno == EINTR)
                continue;
            return -errno;
        }
        cursor += (size_t)read_count;
        length -= (size_t)read_count;
    }
    return 0;
}

static int write_full(int fd, const void *buffer, size_t length)
{
    const unsigned char *cursor = buffer;
    while (length != 0) {
        ssize_t written = write(fd, cursor, length);
        if (written < 0) {
            if (errno == EINTR)
                continue;
            return -errno;
        }
        cursor += (size_t)written;
        length -= (size_t)written;
    }
    return 0;
}

static void unload_object(struct loader_state *state)
{
    /* These inner-map fds are ours (bpf_map_get_fd_by_id); close them or every
       reload leaks four descriptors. */
    for (size_t slot = 0; slot < 2; slot++) {
        if (state->event_fd[slot] >= 0) {
            close(state->event_fd[slot]);
            state->event_fd[slot] = -1;
        }
        if (state->stats_fd[slot] >= 0) {
            close(state->stats_fd[slot]);
            state->stats_fd[slot] = -1;
        }
    }
    for (size_t index = 0; index < 6; index++) {
        if (state->links[index] != NULL) {
            bpf_link__destroy(state->links[index]);
            state->links[index] = NULL;
        }
    }
    if (state->object != NULL) {
        bpf_object__close(state->object);
        state->object = NULL;
    }
    state->events_outer_fd = -1;
    state->stats_outer_fd = -1;
    state->active_slot_fd = -1;
    state->in_flight_fd = -1;
    state->event_fd[0] = -1;
    state->event_fd[1] = -1;
    state->stats_fd[0] = -1;
    state->stats_fd[1] = -1;
    state->ready = false;
}

static int map_fd(struct bpf_object *object, const char *name)
{
    struct bpf_map *map = bpf_object__find_map_by_name(object, name);
    if (map == NULL)
        return -ENOENT;
    return bpf_map__fd(map);
}

static int lookup_inner_maps(struct loader_state *state)
{
    __u32 slot;
    int fd;

    state->events_outer_fd = map_fd(state->object, "fileio_slots");
    state->stats_outer_fd = map_fd(state->object, "fileio_stats_slots");
    state->active_slot_fd = map_fd(state->object, "fileio_active_slot");
    state->in_flight_fd = map_fd(state->object, "fileio_in_flight");
    if (state->events_outer_fd < 0 || state->stats_outer_fd < 0 ||
        state->active_slot_fd < 0 || state->in_flight_fd < 0)
        return -ENOENT;

    for (slot = 0; slot < 2; slot++) {
        /* An ARRAY_OF_MAPS lookup returns the inner map *ID*, not a file
           descriptor. Using the u32 id as an fd made every drain fail with
           EBADF, which the P4 privileged acceptance (T-007) caught here.
           Translate the id into an owned fd. */
        __u32 inner_id = 0;
        if (bpf_map_lookup_elem(state->events_outer_fd, &slot, &inner_id) != 0)
            return -errno;
        if (inner_id == 0)
            return -ENOENT;
        fd = bpf_map_get_fd_by_id(inner_id);
        if (fd < 0)
            return -errno;
        state->event_fd[slot] = fd;

        inner_id = 0;
        if (bpf_map_lookup_elem(state->stats_outer_fd, &slot, &inner_id) != 0)
            return -errno;
        if (inner_id == 0)
            return -ENOENT;
        fd = bpf_map_get_fd_by_id(inner_id);
        if (fd < 0)
            return -errno;
        state->stats_fd[slot] = fd;
    }
    return 0;
}

static int load_object(struct loader_state *state, const char *path)
{
    /* LIBBPF_OPTS sets .sz. A zeroed struct leaves sz = 0 and libbpf >= 1.0
       rejects it with EINVAL ("bpf_object_open_opts size (0) is too small"),
       which made the loader unable to open the object at all. Found by the P4
       privileged acceptance harness (T-007) on this host. */
    LIBBPF_OPTS(bpf_object_open_opts, options);
    int error;

    unload_object(state);
    state->hook_mask = 0;
    state->object = bpf_object__open_file(path, &options);
    error = (int)libbpf_get_error(state->object);
    if (error != 0) {
        state->object = NULL;
        set_diagnostic(state, error, "open BPF object failed: %s", strerror(-error));
        return error;
    }
    error = bpf_object__load(state->object);
    if (error != 0) {
        set_diagnostic(state, error, "BPF verifier/load failed: %s", strerror(-error));
        unload_object(state);
        return error;
    }

    for (size_t index = 0; index < 6; index++) {
        struct bpf_program *program =
            bpf_object__find_program_by_name(state->object, hook_programs[index]);
        if (program == NULL) {
            set_diagnostic(state, -ENOENT, "missing hook program %s", hook_programs[index]);
            unload_object(state);
            return -ENOENT;
        }
        state->links[index] = bpf_program__attach(program);
        error = (int)libbpf_get_error(state->links[index]);
        if (error != 0) {
            state->links[index] = NULL;
            set_diagnostic(state, error, "attach %s failed: %s", hook_programs[index],
                           strerror(-error));
            unload_object(state);
            return error;
        }
        state->hook_mask |= (__u32)1u << index;
    }
    error = lookup_inner_maps(state);
    if (error != 0) {
        set_diagnostic(state, error, "required double-buffer maps unavailable");
        unload_object(state);
        return error;
    }
    state->ready = state->hook_mask == FILEIO_HOOK_MASK_ALL;
    if (!state->ready) {
        set_diagnostic(state, -ENODEV, "not all six VFS hooks attached");
        unload_object(state);
        return -ENODEV;
    }
    state->last_errno = 0;
    state->diagnostic[0] = '\0';
    return 0;
}

static size_t diagnostic_length(const struct loader_state *state)
{
    return strnlen(state->diagnostic, FILEIO_DIAGNOSTIC_CAP);
}

static int send_hello(const struct loader_state *state)
{
    struct fileio_hello_response response = {
        .abi_version = FILEIO_ABI_VERSION,
        .status = state->ready ? FILEIO_STATUS_OK : FILEIO_STATUS_ERROR,
        .hook_mask = state->hook_mask,
        .map_capacity = FILEIO_MAX_MAP_ENTRIES,
        .error = -state->last_errno,
        .verifier_len = (__u32)diagnostic_length(state),
    };
    int error = write_full(STDOUT_FILENO, &response, sizeof(response));
    if (error == 0 && response.verifier_len != 0)
        error = write_full(STDOUT_FILENO, state->diagnostic, response.verifier_len);
    return error;
}

static int send_ack(const struct loader_state *state, __u16 status, int error)
{
    struct fileio_ack_response response = {
        .abi_version = FILEIO_ABI_VERSION,
        .status = status,
        .error = error,
        .diagnostic_len = (__u32)diagnostic_length(state),
    };
    int write_error = write_full(STDOUT_FILENO, &response, sizeof(response));
    if (write_error == 0 && response.diagnostic_len != 0)
        write_error = write_full(STDOUT_FILENO, state->diagnostic, response.diagnostic_len);
    return write_error;
}

static int send_drain_error(const struct loader_state *state,
                            const struct fileio_flip_request *request,
                            __u32 drained_slot, int error)
{
    struct fileio_drain_response response = {
        .abi_version = FILEIO_ABI_VERSION,
        .status = FILEIO_STATUS_ERROR,
        .hook_mask = state->hook_mask,
        .map_capacity = FILEIO_MAX_MAP_ENTRIES,
        .error = error,
        .drained_slot = drained_slot,
        .epoch = request->epoch,
        .seq = request->seq,
        .diagnostic_len = (__u32)diagnostic_length(state),
    };
    int write_error = write_full(STDOUT_FILENO, &response, sizeof(response));
    if (write_error == 0 && response.diagnostic_len != 0)
        write_error = write_full(STDOUT_FILENO, state->diagnostic, response.diagnostic_len);
    return write_error;
}
static int append_record(struct record_vector *records,
                         const struct fileio_key *key,
                         const struct fileio_value *value)
{
    if (records->count == records->capacity) {
        size_t capacity = records->capacity == 0 ? 64 : records->capacity * 2;
        void *items = realloc(records->items, capacity * sizeof(*records->items));
        if (items == NULL)
            return -ENOMEM;
        records->items = items;
        records->capacity = capacity;
    }
    struct fileio_file_record *record = &records->items[records->count++];
    memset(record, 0, sizeof(*record));
    record->cgroup_id = key->cgroup_id;
    /* bd_dev is Linux's internal dev_t (12 major bits, 20 minor bits).
     * It is not libc's differently encoded st_dev. Keep the raw BPF key intact.
     */
    record->major = (__u32)(key->device >> 20);
    record->minor = (__u32)(key->device & ((1U << 20) - 1));
    record->inode = key->inode;
    record->generation = key->i_generation;
    record->reads = value->reads;
    record->writes = value->writes;
    record->bytes_r = value->bytes_r;
    record->bytes_w = value->bytes_w;
    record->name_len = value->name_len > FILEIO_NAME_CAP ? FILEIO_NAME_CAP : value->name_len;
    memcpy(record->name, value->name, FILEIO_NAME_CAP);
    return 0;
}

static int append_stats(struct stats_vector *stats,
                        const struct fileio_cgroup_stats *value)
{
    if (stats->count == stats->capacity) {
        size_t capacity = stats->capacity == 0 ? 32 : stats->capacity * 2;
        void *items = realloc(stats->items, capacity * sizeof(*stats->items));
        if (items == NULL)
            return -ENOMEM;
        stats->items = items;
        stats->capacity = capacity;
    }
    stats->items[stats->count++] = *value;
    return 0;
}

static int clear_event_map(int fd, struct record_vector *records)
{
    for (;;) {
        struct fileio_key key;
        struct fileio_value value;
        int error = bpf_map_get_next_key(fd, NULL, &key);
        if (error == -ENOENT)
            return 0;
        if (error != 0)
            return error;
        error = bpf_map_lookup_elem(fd, &key, &value);
        if (error != 0)
            return error;
        error = append_record(records, &key, &value);
        if (error != 0)
            return error;
        error = bpf_map_delete_elem(fd, &key);
        if (error != 0)
            return error;
    }
}

static int clear_stats_map(int fd, struct stats_vector *stats)
{
    for (;;) {
        __u64 key;
        struct fileio_cgroup_stats value;
        int error = bpf_map_get_next_key(fd, NULL, &key);
        if (error == -ENOENT)
            return 0;
        if (error != 0)
            return error;
        error = bpf_map_lookup_elem(fd, &key, &value);
        if (error != 0)
            return error;
        error = append_stats(stats, &value);
        if (error != 0)
            return error;
        error = bpf_map_delete_elem(fd, &key);
        if (error != 0)
            return error;
    }
}

static int wait_for_quiescence(struct loader_state *state)
{
    const long timeout_ns = 500000000L;
    struct timespec start;
    if (clock_gettime(CLOCK_MONOTONIC, &start) != 0)
        return -errno;

    for (;;) {
        __u32 zero = 0;
        __u64 in_flight = 0;
        int error = bpf_map_lookup_elem(state->in_flight_fd, &zero, &in_flight);
        if (error != 0)
            return error;
        if (in_flight == 0)
            return 0;

        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
            return -errno;
        long elapsed_ns = (now.tv_sec - start.tv_sec) * 1000000000L +
                          (now.tv_nsec - start.tv_nsec);
        if (elapsed_ns >= timeout_ns)
            return -ETIMEDOUT;
        struct timespec delay = {.tv_sec = 0, .tv_nsec = 1000000L};
        nanosleep(&delay, NULL);
    }
}

static int drain(struct loader_state *state, const struct fileio_flip_request *request)
{
    __u32 zero = 0;
    __u32 active = 0;
    __u32 old_slot = 0;
    struct record_vector records = {};
    struct stats_vector stats = {};
    int error;

    if (!state->ready) {
        error = -ENODEV;
        goto report_error;
    }
    if (state->draining) {
        error = -EBUSY;
        goto report_error;
    }

    error = bpf_map_lookup_elem(state->active_slot_fd, &zero, &active);
    if (error != 0)
        goto report_error;
    if (active > 1) {
        error = -EINVAL;
        goto report_error;
    }
    old_slot = active;
    __u32 new_slot = 1 - active;
    error = bpf_map_update_elem(state->active_slot_fd, &zero, &new_slot, BPF_ANY);
    if (error != 0)
        goto report_error;

    state->draining = true;
    error = wait_for_quiescence(state);
    if (error == 0)
        error = clear_event_map(state->event_fd[old_slot], &records);
    if (error == 0)
        error = clear_stats_map(state->stats_fd[old_slot], &stats);
    if (error != 0) {
        state->ready = false;
        goto report_error;
    }

    struct fileio_drain_response response = {
        .abi_version = FILEIO_ABI_VERSION,
        .status = FILEIO_STATUS_OK,
        .hook_mask = state->hook_mask,
        .map_capacity = FILEIO_MAX_MAP_ENTRIES,
        .error = 0,
        .drained_slot = old_slot,
        .epoch = request->epoch,
        .seq = request->seq,
        .event_count = (__u32)records.count,
        .stats_count = (__u32)stats.count,
        .complete = FILEIO_DRAIN_COMPLETE,
        .diagnostic_len = 0,
    };
    error = write_full(STDOUT_FILENO, &response, sizeof(response));
    if (error == 0)
        error = write_full(STDOUT_FILENO, records.items,
                           records.count * sizeof(*records.items));
    if (error == 0)
        error = write_full(STDOUT_FILENO, stats.items,
                           stats.count * sizeof(*stats.items));
    free(records.items);
    free(stats.items);
    state->draining = false;
    return error;

report_error:
    set_diagnostic(state, error,
                   "DRAIN failed before acknowledgement: quiescence/enumeration/clear: %s",
                   strerror(-error));
    free(records.items);
    free(stats.items);
    state->draining = false;
    return send_drain_error(state, request, old_slot, error);
}

static int discard_payload(int fd, __u32 length)
{
    unsigned char buffer[256];
    while (length != 0) {
        size_t amount = length < sizeof(buffer) ? length : sizeof(buffer);
        int error = read_full(fd, buffer, amount);
        if (error != 0)
            return error;
        length -= (__u32)amount;
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *object_path = argc > 1 ? argv[1] : "fileio.bpf.o";
    struct loader_state state = {
        .events_outer_fd = -1,
        .stats_outer_fd = -1,
        .active_slot_fd = -1,
        .in_flight_fd = -1,
        .event_fd = {-1, -1},
        .stats_fd = {-1, -1},
    };
    libbpf_set_print(loader_log);

    for (;;) {
        struct fileio_command_header command;
        int error = read_full(STDIN_FILENO, &command, sizeof(command));
        if (error != 0)
            break;
        if (command.abi_version != FILEIO_ABI_VERSION) {
            set_diagnostic(&state, EPROTO, "unsupported ABI version %u", command.abi_version);
            if (discard_payload(STDIN_FILENO, command.payload_len) != 0)
                break;
            if (send_ack(&state, FILEIO_STATUS_ERROR, -EPROTO) != 0)
                break;
            continue;
        }
        if (command.opcode == FILEIO_OP_HELLO) {
            if (command.payload_len != 0 && discard_payload(STDIN_FILENO, command.payload_len) != 0)
                break;
            if (!state.ready)
                (void)load_object(&state, object_path);
            if (send_hello(&state) != 0)
                break;
        } else if (command.opcode == FILEIO_OP_FLIP_DRAIN) {
            struct fileio_flip_request request;
            if (command.payload_len != sizeof(request)) {
                if (discard_payload(STDIN_FILENO, command.payload_len) != 0)
                    break;
                set_diagnostic(&state, EPROTO, "invalid FLIP_DRAIN payload length");
                if (send_ack(&state, FILEIO_STATUS_ERROR, -EPROTO) != 0)
                    break;
                continue;
            }
            if (read_full(STDIN_FILENO, &request, sizeof(request)) != 0)
                break;
            if (drain(&state, &request) != 0)
                break;
        } else if (command.opcode == FILEIO_OP_CLOSE) {
            if (discard_payload(STDIN_FILENO, command.payload_len) != 0)
                break;
            int close_error = state.draining ? -EBUSY : 0;
            if (close_error == 0)
                unload_object(&state);
            if (send_ack(&state, close_error == 0 ? FILEIO_STATUS_OK : FILEIO_STATUS_ERROR,
                         close_error) != 0)
                break;
            break;
        } else {
            if (discard_payload(STDIN_FILENO, command.payload_len) != 0)
                break;
            set_diagnostic(&state, EINVAL, "unknown loader opcode %u", command.opcode);
            if (send_ack(&state, FILEIO_STATUS_ERROR, -EINVAL) != 0)
                break;
        }
    }

    unload_object(&state);
    return 0;
}
