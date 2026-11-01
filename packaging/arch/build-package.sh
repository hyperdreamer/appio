#!/usr/bin/env bash
# Export this checkout into a deterministic source tarball, verify the digest
# recorded in PKGBUILD, then build the package with makepkg.
#
#   ./packaging/arch/build-package.sh            # verify + build
#   ./packaging/arch/build-package.sh --update   # re-record the digest, then build
#   ./packaging/arch/build-package.sh --install  # build, then pacman -U
#
# The tarball holds the working tree (so an uncommitted, reviewed change can be
# packaged) but excludes VCS metadata, caches and this packaging directory.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
pkgbuild_src="$here/PKGBUILD"
builddir=${APPIO_PKGBUILD_DIR:-/var/tmp/appio-archbuild}

update_digest=0
install_after=0
for arg in "$@"; do
  case $arg in
    --update) update_digest=1 ;;
    --install) install_after=1 ;;
    -h|--help)
      sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      printf 'unknown option: %s\n' "$arg" >&2
      exit 2
      ;;
  esac
done

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

mkdir -p "$builddir"
pkgver=$pkgbuild_ver
tarball="$builddir/appio-$pkgver.tar.gz"
mtime=$(git -C "$repo" log -1 --format=%ct)

# -n keeps gzip from stamping the current time, and fixed ownership/mtime keep
# the digest stable while the tree is unchanged. The exclude patterns match
# single path components, so .gitignore and friends stay in the export.
tar --create --use-compress-program='gzip -9n' --file "$tarball" \
  --directory "$repo" \
  --exclude=.git --exclude=packaging --exclude=__pycache__ \
  --exclude='*.pyc' --exclude=.pytest_cache --exclude=.codegraph \
  --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$mtime" \
  --transform "s,^\.,appio-$pkgver," .

digest=$(sha256sum "$tarball" | cut -d' ' -f1)
recorded=$(sed -n "s/^sha256sums=('\([0-9a-f]*\)').*/\1/p" "$pkgbuild_src" | head -1)
if [[ $digest != "$recorded" ]]; then
  if ((update_digest)); then
    sed -i "s/^sha256sums=('.*')/sha256sums=('$digest')/" "$pkgbuild_src"
    recorded=$digest
    printf 'recorded new sha256 %s in %s\n' "$digest" "$pkgbuild_src"
  else
    printf 'source digest changed:\n  tarball: %s\n  PKGBUILD: %s\n' \
      "$digest" "${recorded:-<none>}" >&2
    printf 'rerun with --update to re-record it\n' >&2
    exit 1
  fi
fi

cp "$pkgbuild_src" "$builddir/PKGBUILD"
cd "$builddir"
makepkg --force --cleanbuild --syncdeps --noconfirm
package=$(makepkg --packagelist | head -1)

if ((install_after)); then
  sudo pacman -U --noconfirm "$package"
fi
printf '\nbuilt: %s\n' "$package"
