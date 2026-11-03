#!/usr/bin/env bash
# Export this checkout into a deterministic source tarball, verify the digest
# recorded in PKGBUILD, then build the package with makepkg.
#
#   ./packaging/arch/build-package.sh            # verify + build
#   ./packaging/arch/build-package.sh --check    # verify the digest, build nothing
#   ./packaging/arch/build-package.sh --update   # re-record the digest, then build
#   ./packaging/arch/build-package.sh --install  # build, then pacman -U
#
# The tarball holds the tracked working tree, so an uncommitted but reviewed
# change can still be packaged, while VCS metadata, caches and this packaging
# directory stay out.
#
# The file list comes from `git ls-files`, not from walking the directory.
# Walking it packages whatever happens to be lying around -- an ignored
# .worktrees/ directory, a stray note, an editor backup -- and each of those
# changes the digest without changing a single packaged source file. That is how
# the recorded value rotted three separate times here: a pkgver bump, a
# .gitignore edit, and a transient worktree directory. Because the list is
# tracked-only, an untracked file would now be dropped silently, so it is
# refused outright instead.
#
# A pkgver bump still invalidates the digest, because the export embeds it in
# every path. Record it once per release: bump pkgver, run --update, commit the
# result. That commit cannot perturb the digest again: packaging/ is excluded
# from the export, and the tarball timestamp is fixed rather than derived from
# git, so the digest is a function of packaged content and pkgver alone.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd -- "$here/../.." && pwd)
pkgbuild_src="$here/PKGBUILD"
builddir=${APPIO_PKGBUILD_DIR:-/var/tmp/appio-archbuild}

usage() {
  cat <<'EOF'
usage: build-package.sh [--check] [--update] [--install]

  --check    verify the digest recorded in PKGBUILD, build nothing
  --update   re-record the digest for the current tree, then build
  --install  build, then pacman -U the result
EOF
}

update_digest=0
install_after=0
check_only=0
for arg in "$@"; do
  case $arg in
    --check) check_only=1 ;;
    --update) update_digest=1 ;;
    --install) install_after=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      printf 'unknown option: %s\n' "$arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
  printf '%s is not a git checkout; the export needs git to list tracked files\n' \
    "$repo" >&2
  exit 1
fi

pyproject_ver=$(python - "$repo/pyproject.toml" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
PY
)
pkgbuild_ver=$(sed -n 's/^pkgver=//p' "$pkgbuild_src" | head -1)
if [[ $pyproject_ver != "$pkgbuild_ver" ]]; then
  printf 'pkgver %s in PKGBUILD does not match %s in pyproject.toml\n' \
    "$pkgbuild_ver" "$pyproject_ver" >&2
  exit 1
fi

# Tracked-only export means an untracked file would be omitted without a word.
# Refuse instead: it is always either work to commit or scratch to ignore.
untracked=()
while IFS= read -r -d '' path; do untracked+=("$path"); done \
  < <(git -C "$repo" ls-files --others --exclude-standard -z)
if ((${#untracked[@]})); then
  printf 'refusing to package: untracked files would be dropped from the tarball\n' >&2
  printf '  %s\n' "${untracked[@]}" >&2
  printf 'commit them, or add them to .gitignore if they are local scratch\n' >&2
  exit 1
fi

missing=()
while IFS= read -r -d '' path; do missing+=("$path"); done \
  < <(git -C "$repo" ls-files --deleted -z)
if ((${#missing[@]})); then
  printf 'refusing to package: tracked files are absent from the working tree\n' >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

mkdir -p "$builddir"
pkgver=$pkgbuild_ver
tarball="$builddir/appio-$pkgver.tar.gz"

# A fixed timestamp, never the last commit's. Deriving it from HEAD made every
# commit invalidate the digest -- including the commit that records the digest,
# so recording it and committing it could never converge. With it fixed, the
# digest describes packaged content and pkgver and nothing else; SOURCE_DATE_EPOCH
# still lets a distributor pin a release date deliberately.
mtime=${SOURCE_DATE_EPOCH:-0}

# -n keeps gzip from stamping the current time, and fixed ownership/mtime keep
# the digest stable while the tracked tree is unchanged. The exclude patterns
# match single path components, so .gitignore and friends stay in the export.
git -C "$repo" ls-files -z \
  | tar --null --create --use-compress-program='gzip -9n' --file "$tarball" \
    --directory "$repo" \
    --exclude=packaging --exclude=__pycache__ --exclude='*.pyc' \
    --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$mtime" \
    --transform "s,^,appio-$pkgver/," -T -

digest=$(sha256sum "$tarball" | cut -d' ' -f1)
recorded=$(sed -n "s/^sha256sums=('\([0-9a-f]*\)').*/\1/p" "$pkgbuild_src" | head -1)
if [[ $digest != "$recorded" ]]; then
  if ((update_digest)); then
    sed -i "s/^sha256sums=('.*')/sha256sums=('$digest')/" "$pkgbuild_src"
    printf 'recorded new sha256 %s in %s\n' "$digest" "$pkgbuild_src"
  else
    printf 'source digest changed:\n  tarball: %s\n  PKGBUILD: %s\n' \
      "$digest" "${recorded:-<none>}" >&2
    printf 'rerun with --update to re-record it\n' >&2
    exit 1
  fi
fi

if ((check_only)); then
  printf 'digest verified: %s\n' "$digest"
  exit 0
fi

cp "$pkgbuild_src" "$builddir/PKGBUILD"
cd "$builddir"
makepkg --force --cleanbuild --syncdeps --noconfirm
package=$(makepkg --packagelist | head -1)

if ((install_after)); then
  sudo pacman -U --noconfirm "$package"
fi
printf '\nbuilt: %s\n' "$package"
