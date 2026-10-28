CLANG ?= clang
CC ?= cc
OUT_DIR ?= $(CURDIR)/build
LIBBPF_CFLAGS := $(shell pkg-config --cflags libbpf)
LIBBPF_LIBS := $(shell pkg-config --libs libbpf) -lelf -lz
BPF_CFLAGS := -target bpf -D__TARGET_ARCH_x86 -O2 -g -std=gnu11 -Wall -Wextra -I.
USER_CFLAGS := -O2 -g -std=c11 -Wall -Wextra -Werror -I.

BPF_OBJECT := $(OUT_DIR)/fileio.bpf.o
LOADER := $(OUT_DIR)/fileio_loader

.PHONY: all check clean

all: check

check: $(BPF_OBJECT) $(LOADER)

$(OUT_DIR):
	mkdir -p "$@"

$(BPF_OBJECT): fileio.bpf.c fileio_protocol.h fileio_types.h | $(OUT_DIR)
	$(CLANG) $(BPF_CFLAGS) -c "$<" -o "$@"

$(LOADER): fileio_loader.c fileio_protocol.h | $(OUT_DIR)
	$(CC) $(USER_CFLAGS) $(LIBBPF_CFLAGS) "$<" -o "$@" $(LIBBPF_LIBS)

clean:
	rm -rf "$(OUT_DIR)"
