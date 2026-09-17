#!/usr/bin/env bash
set -Eeuo pipefail

PROGRAM="kvm"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PATCH_DIR="$SCRIPT_DIR/kvm/patches"
BACKUP_ROOT="${OVO_KVM_BACKUP_ROOT:-/var/backups/ovo-kvm}"
WORK_ROOT="${OVO_KVM_WORK_ROOT:-${XDG_CACHE_HOME:-$HOME/.cache}/ovo-kvm}"
SOURCE_ARCHIVE=""
SOURCE_URL=""
SOURCE_B2SUM=""
SOURCE_PATCH_URLS=()
SOURCE_PATCH_B2SUMS=()
SOURCE_PATCH_FILES=()
PATCH_FILE=""
ROLLBACK_DIR=""
ACTION=""
JOBS="${OVO_KVM_JOBS:-$(nproc)}"
BUILD_ONLY=0
ASSUME_YES=0
SKIP_DEPS=0
REBUILD=0
BUILD_FORMAT=5
BUILD_TOOLCHAIN=""
BUILD_COMPILER=""
BUILD_STRIP=""
KBUILD_MAKE_ARGS=()
KBUILD_MAKE_ARGS_TEXT=""

SOURCE_SIGNERS=(
    E18447AC260021D31F3FF6C4C8A2A4774B8B63C4
    E8B9AA39F054E30E8290D492C3C4820857F654FE
)

log() { printf '[%s] %s\n' "$PROGRAM" "$*"; }
die() { printf '[%s] ERROR: %s\n' "$PROGRAM" "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
Usage:
  ./kvm.sh
  ./kvm.sh install [options]
  ./kvm.sh restore [backup-directory]

With no arguments, show a menu:
  1) Install KVM patch
  2) Restore backup

Install options:
  --patch FILE           Override kvm/patches/<package-version>.<intel|amd>.patch.
  --source-archive FILE  Use an existing official source archive.
  --work-dir DIR         Cache and build directory.
  --jobs N               Parallel build jobs.
  --build-only           Build and stage modules without installing them.
  --skip-deps            Do not install missing packages.
  --rebuild              Recreate this patch version's build tree.
  --yes                  Skip the final installation confirmation.
  -h, --help             Show this help.

Run as a normal user. sudo is used to install or restore modules and to
write backups under /var/backups/ovo-kvm.
A reboot is required after install or restore.
EOF
}

backup_relpath() {
    local dir=$1
    printf '%s\n' "${dir#"$BACKUP_ROOT"/}"
}

list_backup_dirs() {
    local dir
    [[ -d "$BACKUP_ROOT" ]] || return 0
    local entries=()
    shopt -s nullglob
    for dir in "$BACKUP_ROOT"/*/; do
        dir=${dir%/}
        [[ -d "$dir" && -f "$dir/SHA256SUMS" ]] || continue
        entries+=("$dir")
    done
    shopt -u nullglob
    ((${#entries[@]})) || return 0
    printf '%s\0' "${entries[@]}" | xargs -0 ls -1dt
}

pick_backup_interactive() {
    local backups=() dir i choice meta
    mapfile -t backups < <(list_backup_dirs)
    ((${#backups[@]})) || die "no backups found in $BACKUP_ROOT"
    printf '\nAvailable backups in %s:\n' "$BACKUP_ROOT"
    for i in "${!backups[@]}"; do
        dir=${backups[$i]}
        meta=""
        if [[ -f "$dir/METADATA" ]]; then
            meta=$(tr '\n' ' ' < "$dir/METADATA")
        fi
        printf '  %d) %s\n' "$((i + 1))" "$(backup_relpath "$dir")"
        if [[ -n "$meta" ]]; then
            printf '      %s\n' "$meta"
        fi
    done
    printf '  0) Cancel\n'
    while true; do
        read -r -p 'Restore which backup? ' choice || exit 0
        [[ "$choice" =~ ^[0-9]+$ ]] || { printf 'invalid choice: %s\n' "${choice:-<empty>}" >&2; continue; }
        if [[ "$choice" == 0 ]]; then
            log "restore cancelled"
            exit 0
        fi
        ((choice >= 1 && choice <= ${#backups[@]})) || { printf 'invalid choice: %s\n' "$choice" >&2; continue; }
        ROLLBACK_DIR="${backups[$((choice - 1))]}"
        return
    done
}

prompt_menu() {
    local reply
    while true; do
        printf '\nOVO KVM\n'
        printf '  1) Install KVM patch\n'
        printf '  2) Restore backup\n'
        printf '  q) Quit\n'
        read -r -p '> ' reply || exit 0
        case "$reply" in
            1)
                ACTION=install
                return
                ;;
            2)
                ACTION=restore
                pick_backup_interactive
                return
                ;;
            q|Q)
                exit 0
                ;;
            *)
                printf 'invalid choice: %s\n' "${reply:-<empty>}" >&2
                ;;
        esac
    done
}

if (($# == 0)); then
    prompt_menu
else
    case "$1" in
        install) ACTION=install; shift ;;
        restore)
            ACTION=restore
            shift
            if (($#)) && [[ "$1" != -* ]]; then ROLLBACK_DIR=$1; shift; else ROLLBACK_DIR=""; fi
            ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown command: $1 (use ./kvm.sh, ./kvm.sh install, or ./kvm.sh restore)" ;;
    esac
fi

while (($#)); do
    case "$1" in
        --patch) [[ "$ACTION" == install ]] || die "--patch is only valid with install"
            [[ $# -ge 2 ]] || die "--patch requires a file"; PATCH_FILE=$2; shift 2 ;;
        --source-archive) [[ "$ACTION" == install ]] || die "--source-archive is only valid with install"
            [[ $# -ge 2 ]] || die "--source-archive requires a file"; SOURCE_ARCHIVE=$2; shift 2 ;;
        --work-dir) [[ $# -ge 2 ]] || die "--work-dir requires a directory"; WORK_ROOT=$2; shift 2 ;;
        --jobs) [[ $# -ge 2 ]] || die "--jobs requires a positive integer"; JOBS=$2; shift 2 ;;
        --build-only) [[ "$ACTION" == install ]] || die "--build-only is only valid with install"
            BUILD_ONLY=1; shift ;;
        --skip-deps) [[ "$ACTION" == install ]] || die "--skip-deps is only valid with install"
            SKIP_DEPS=1; shift ;;
        --rebuild) [[ "$ACTION" == install ]] || die "--rebuild is only valid with install"
            REBUILD=1; shift ;;
        --yes) [[ "$ACTION" == install ]] || die "--yes is only valid with install"
            ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die "jobs must be a positive integer"
command -v sudo >/dev/null 2>&1 || ((EUID == 0)) || die "sudo is required"
if ((EUID == 0)); then SUDO=(); else SUDO=(sudo); fi
root_run() { "${SUDO[@]}" "$@"; }

mkdir -p "$WORK_ROOT"
WORK_ROOT=$(realpath "$WORK_ROOT")

acquire_lock() {
    command -v flock >/dev/null 2>&1 || die "flock is required"
    exec 9>"$WORK_ROOT/.install.lock"
    flock -n 9 || die "another installer instance is running"
}

KERNEL_RELEASE=$(uname -r)
MODULE_ROOT="/usr/lib/modules/$KERNEL_RELEASE"
MODULE_DIR="$MODULE_ROOT/kernel/arch/x86/kvm"
KVM_VENDOR=""
KVM_KO=""
KVM_MODINFO_NAME=""
LOCAL_KERNEL=0

detect_cpu_vendor() {
    local vendor
    vendor=$(awk -F: '/^vendor_id/ { gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2; exit }' /proc/cpuinfo)
    case "$vendor" in
        GenuineIntel)
            KVM_VENDOR=intel
            KVM_KO=kvm-intel
            ;;
        AuthenticAMD)
            KVM_VENDOR=amd
            KVM_KO=kvm-amd
            ;;
        *)
            die "unsupported CPU vendor '$vendor' (expected GenuineIntel or AuthenticAMD)"
            ;;
    esac
    KVM_MODINFO_NAME=${KVM_KO//-/_}
    log "detected $KVM_VENDOR CPU; will patch and install kvm.ko + ${KVM_KO}.ko"
}

detect_kernel_package() {
    command -v pacman >/dev/null 2>&1 || die "this installer requires a pacman-based CachyOS system"
    [[ -d "$MODULE_ROOT/kernel" ]] || die "module tree not found for $KERNEL_RELEASE"
    KERNEL_PACKAGE=$(pacman -Qoq "$MODULE_ROOT/kernel" 2>/dev/null | head -n1 || true)
    if [[ -n "$KERNEL_PACKAGE" ]]; then
        KERNEL_PACKAGE_VERSION=$(pacman -Q "$KERNEL_PACKAGE" | awk '{print $2}')
        HEADER_PACKAGE="${KERNEL_PACKAGE}-headers"
    elif [[ "$KERNEL_RELEASE" == "6.19.14-ovo" ]]; then
        LOCAL_KERNEL=1
        KERNEL_PACKAGE="linux619-local"
        KERNEL_PACKAGE_VERSION="$KERNEL_RELEASE"
        HEADER_PACKAGE=""
        log "using supported local kernel $KERNEL_RELEASE (not owned by pacman)"
    else
        die "could not determine the package owning $MODULE_ROOT/kernel"
    fi
    HEADER_TREE="$MODULE_ROOT/build"
    [[ -f "$HEADER_TREE/Makefile" ]] \
        || die "header tree not found for running kernel $KERNEL_RELEASE"
    [[ "$(make -s -C "$HEADER_TREE" kernelrelease)" == "$KERNEL_RELEASE" ]] \
        || die "header tree does not match running kernel $KERNEL_RELEASE"
    case "$KERNEL_PACKAGE_VERSION" in
        6.19.14-ovo)
            SOURCE_ID="linux-6.19.14"
            SOURCE_URL="https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.19.14.tar.xz"
            SOURCE_B2SUM="64c2a0003d8080f268772d36923ff6ef8b2d55320ea08b77ad39384c98c9a5c1a8e71425470619aa3aa4dda8941f46aa9da364748cfa8fd9f8507a5ddd7ac03a"
            ;;
        6.19.rc6-1)
            SOURCE_ID="linux-6.19-rc6"
            SOURCE_URL="https://github.com/torvalds/linux/archive/refs/tags/v6.19-rc6.tar.gz"
            SOURCE_B2SUM="536754b09ed4460b138eb18c3c524b58500cc73b1ba4ce557fc18eccab289b5e010f952e8fe112edd18d77d1b7bfc85ffbda969c1c0c130ebd9ffc514e737bf8"
            SOURCE_PATCH_URLS=(
                "https://raw.githubusercontent.com/CachyOS/kernel-patches/cec2d1841baae411313742083ef2bc0b29855b4d/6.19/all/0001-cachyos-base-all.patch"
                "https://raw.githubusercontent.com/CachyOS/kernel-patches/cec2d1841baae411313742083ef2bc0b29855b4d/6.19/sched/0001-bore-cachy.patch"
                "https://raw.githubusercontent.com/CachyOS/kernel-patches/cec2d1841baae411313742083ef2bc0b29855b4d/6.19/misc/dkms-clang.patch"
            )
            SOURCE_PATCH_B2SUMS=(
                "88cf195c604d7b9b398037990dbf02474c3326e31aed45514aa003c6b3d3d5b788a41ac0f670a12721a041067531e98c3583d3b49192bdd9cc04a99495fcf2a3"
                "3b5cd9589b318a848af2f53aad0ea8ae56932900346ae1f884e9667f85f9bd09389726cd4902c54fcb094de278222199b079cafaec9c620ea91f47cc91c7fe3b"
                "ea26c88950fc06b6ffab93b30e3beacc7d26571a70262334ca8b001dc7899bf96b47d703fbaa7f4e47765c3dafccc23c58a4d4da2169b8ee50012afcb7a1dd96"
            )
            ;;
        *)
            SOURCE_ID="cachyos-${KERNEL_PACKAGE_VERSION}"
            ;;
    esac
}

select_patch_file() {
    local candidate="$PATCH_DIR/${KERNEL_PACKAGE_VERSION}.${KVM_VENDOR}.patch"
    if [[ -n "$PATCH_FILE" ]]; then
        [[ -f "$PATCH_FILE" ]] || die "no matching OVO patch: $PATCH_FILE"
        PATCH_FILE=$(realpath "$PATCH_FILE")
        return
    fi
    log "kernel $KERNEL_PACKAGE_VERSION, CPU $KVM_VENDOR; looking for $(basename "$candidate")"
    if [[ ! -f "$candidate" ]]; then
        die "no matching patch: $candidate
Add kvm/patches/${KERNEL_PACKAGE_VERSION}.${KVM_VENDOR}.patch for this kernel and CPU, then rerun."
    fi
    PATCH_FILE=$(realpath "$candidate")
    log "using patch $PATCH_FILE"
}

check_idle() {
    if root_run fuser -s /dev/kvm 2>/dev/null; then
        die "/dev/kvm is in use; stop all virtual machines first"
    fi
    if pgrep -af '(^|/)(qemu-system-[^ ]*|qemu-kvm)( |$)' >/dev/null 2>&1; then
        die "a QEMU process is running; stop all virtual machines first"
    fi
}

initramfs_contains_kvm() {
    command -v lsinitcpio >/dev/null 2>&1 || return 1
    local image
    for image in /boot/initramfs*.img; do
        [[ -f "$image" ]] || continue
        if root_run lsinitcpio "$image" 2>/dev/null | grep -E '(^|/)(kvm|kvm-intel|kvm-amd)\.ko(\.zst)?([[:space:]]|$)' >/dev/null; then
            return 0
        fi
    done
    return 1
}

restore_backup() {
    local backup=$1
    [[ -d "$backup" ]] || die "backup directory not found: $backup"
    [[ -s "$backup/SHA256SUMS" ]] || die "backup manifest not found: $backup/SHA256SUMS"
    (cd "$backup" && sha256sum -c SHA256SUMS >/dev/null) \
        || die "backup checksum verification failed: $backup"
    root_run install -d -m 0755 "$MODULE_DIR"
    local file
    while read -r file; do
        [[ -n "$file" ]] || continue
        [[ -f "$backup/$file" ]] || die "backup file missing: $backup/$file"
        root_run install -m 0644 "$backup/$file" "$MODULE_DIR/$file"
    done < <(awk '{print $2}' "$backup/SHA256SUMS")
    root_run depmod "$KERNEL_RELEASE"
    if initramfs_contains_kvm; then root_run mkinitcpio -P; fi
}

last_backup_path() {
    printf '%s\n' "$BACKUP_ROOT/.last-${KERNEL_RELEASE}"
}

resolve_backup_dir() {
    local dir=$1
    [[ -n "$dir" ]] || die "backup directory not specified"
    if [[ ! -d "$dir" && -d "$BACKUP_ROOT/$dir" ]]; then
        dir="$BACKUP_ROOT/$dir"
    fi
    [[ -d "$dir" && -f "$dir/SHA256SUMS" ]] || die "backup directory not found: $dir"
    realpath "$dir"
}

rollback() {
    local backup
    if [[ -z "$ROLLBACK_DIR" ]]; then
        pick_backup_interactive
    fi
    detect_kernel_package
    backup=$(resolve_backup_dir "$ROLLBACK_DIR")
    acquire_lock
    root_run true
    check_idle
    restore_backup "$backup"
    log "restore complete: $backup"
    log "reboot to load the restored modules"
}

detect_build_toolchain() {
    local config="$HEADER_TREE/.config"

    [[ -f "$config" ]] || die "kernel configuration not found for $KERNEL_RELEASE"
    if grep -qx 'CONFIG_CC_IS_CLANG=y' "$config"; then
        grep -qx 'CONFIG_AS_IS_LLVM=y' "$config" \
            || die "Clang kernel uses a non-LLVM assembler; unsupported mixed toolchain"
        grep -qx 'CONFIG_LD_IS_LLD=y' "$config" \
            || die "Clang kernel uses a non-LLD linker; unsupported mixed toolchain"
        BUILD_TOOLCHAIN=clang-llvm
        BUILD_COMPILER=clang
        BUILD_STRIP=llvm-strip
        KBUILD_MAKE_ARGS=(LLVM=1)
        KBUILD_MAKE_ARGS_TEXT='LLVM=1'
    elif grep -qx 'CONFIG_CC_IS_GCC=y' "$config"; then
        grep -qx 'CONFIG_AS_IS_GNU=y' "$config" \
            || die "GCC kernel uses a non-GNU assembler; unsupported mixed toolchain"
        grep -qx 'CONFIG_LD_IS_BFD=y' "$config" \
            || die "GCC kernel uses a non-BFD linker; unsupported mixed toolchain"
        BUILD_TOOLCHAIN=gcc-binutils
        BUILD_COMPILER=gcc
        BUILD_STRIP=strip
        KBUILD_MAKE_ARGS=()
        KBUILD_MAKE_ARGS_TEXT=""
        unset LLVM LLVM_IAS
    else
        die "could not identify the compiler used for $KERNEL_RELEASE"
    fi
}

install_dependencies() {
    local packages=(
        base-devel bc binutils cpio curl gettext git gnupg kmod libelf
        openssl pahole perl procps-ng psmisc python rust rust-bindgen
        rust-src tar util-linux xxhash xz zlib zstd
    )
    local required=(
        awk b2sum bsdtar curl flock fuser git gpg make modinfo pgrep
        sha256sum zstd
    )

    detect_build_toolchain
    if [[ "$BUILD_TOOLCHAIN" == clang-llvm ]]; then
        packages+=(clang llvm lld)
        required+=(clang ld.lld llvm-ar llvm-nm llvm-objcopy llvm-objdump
                   llvm-readelf llvm-strip)
    else
        required+=(gcc ld as ar nm objcopy objdump readelf strip)
    fi
    [[ -z "$HEADER_PACKAGE" ]] || packages+=("$HEADER_PACKAGE")
    local missing=() package
    for package in "${packages[@]}"; do
        pacman -Q "$package" >/dev/null 2>&1 || missing+=("$package")
    done
    if ((${#missing[@]})); then
        ((SKIP_DEPS == 0)) || die "missing packages: ${missing[*]}"
        log "installing build dependencies: ${missing[*]}"
        root_run pacman -S --needed --noconfirm "${missing[@]}"
    fi
    local command
    for command in "${required[@]}"; do
        command -v "$command" >/dev/null 2>&1 || die "required command not found after dependency install: $command"
    done
    [[ -f "$HEADER_TREE/.config" && -f "$HEADER_TREE/Module.symvers" && -f "$HEADER_TREE/vmlinux" ]] \
        || die "matching headers for $KERNEL_RELEASE are unavailable; reboot into the installed kernel and rerun"
    [[ "$(make -s -C "$HEADER_TREE" "${KBUILD_MAKE_ARGS[@]}" kernelrelease)" == "$KERNEL_RELEASE" ]] \
        || die "header tree does not match the running kernel; reboot and rerun"
    local configured_cc actual_cc
    configured_cc=$(sed -n 's/^CONFIG_CC_VERSION_TEXT="\(.*\)"$/\1/p' "$HEADER_TREE/.config")
    actual_cc=$("$BUILD_COMPILER" --version | head -n1)
    [[ -n "$configured_cc" && "$actual_cc" == "$configured_cc" ]] \
        || die "compiler mismatch: kernel used '$configured_cc', current compiler is '$actual_cc'"
    log "selected $BUILD_TOOLCHAIN: $actual_cc"
}

check_environment() {
    local available
    available=$(df --output=avail -B1 "$WORK_ROOT" | tail -n1 | tr -d ' ')
    ((available >= 8 * 1024 * 1024 * 1024)) \
        || die "at least 8 GiB of free space is required under $WORK_ROOT"
    if [[ -r /sys/module/module/parameters/sig_enforce ]] \
       && [[ "$(< /sys/module/module/parameters/sig_enforce)" == Y ]]; then
        die "kernel module signature enforcement is enabled; unsigned local modules cannot be installed"
    fi
}

verify_source_signature() {
    local archive=$1 signature=$2 status fingerprint valid=0
    local gpg_home="$WORK_ROOT/gnupg"
    install -d -m 0700 "$gpg_home"
    for fingerprint in "${SOURCE_SIGNERS[@]}"; do
        gpg --homedir "$gpg_home" --batch --list-keys "$fingerprint" >/dev/null 2>&1 \
            || gpg --homedir "$gpg_home" --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys "$fingerprint" \
            || log "warning: could not fetch optional release key $fingerprint"
    done
    status=$(gpg --homedir "$gpg_home" --batch --status-fd=1 --verify "$signature" "$archive" 2>&1) \
        || die "official source signature verification failed"
    for fingerprint in "${SOURCE_SIGNERS[@]}"; do
        if grep -F "[GNUPG:] VALIDSIG $fingerprint " <<<"$status" >/dev/null; then valid=1; break; fi
    done
    ((valid)) || die "source was not signed by an allowed CachyOS release key"
}

obtain_source_archive() {
    local downloads="$WORK_ROOT/downloads"
    local source_url="${SOURCE_URL:-https://github.com/CachyOS/linux/releases/download/${SOURCE_ID}/${SOURCE_ID}.tar.gz}"
    local signature_url="${source_url}.asc"
    local signature="$downloads/${SOURCE_ID}.tar.gz.asc"
    mkdir -p "$downloads"
    if [[ -z "$SOURCE_ARCHIVE" ]]; then
        SOURCE_ARCHIVE="$downloads/${SOURCE_ID}.tar.gz"
        if [[ -n "$SOURCE_B2SUM" && -f "$SOURCE_ARCHIVE" ]] \
           && [[ "$(b2sum "$SOURCE_ARCHIVE" | awk '{print $1}')" != "$SOURCE_B2SUM" ]]; then
            log "discarding cached source with an invalid checksum: $SOURCE_ARCHIVE"
            rm -f "$SOURCE_ARCHIVE"
        fi
        if [[ ! -f "$SOURCE_ARCHIVE" ]]; then
            log "downloading official source: $SOURCE_ID"
            if [[ -n "$SOURCE_B2SUM" ]]; then
                curl -fL --retry 3 "$source_url" -o "$SOURCE_ARCHIVE.part"
            elif [[ -f "$SOURCE_ARCHIVE.part" ]]; then
                curl -fL --retry 3 -C - "$source_url" -o "$SOURCE_ARCHIVE.part"
            else
                curl -fL --retry 3 "$source_url" -o "$SOURCE_ARCHIVE.part"
            fi
            mv "$SOURCE_ARCHIVE.part" "$SOURCE_ARCHIVE"
        fi
    fi
    [[ -f "$SOURCE_ARCHIVE" ]] || die "source archive not found: $SOURCE_ARCHIVE"
    SOURCE_ARCHIVE=$(realpath "$SOURCE_ARCHIVE")
    if [[ -n "$SOURCE_B2SUM" ]]; then
        [[ "$(b2sum "$SOURCE_ARCHIVE" | awk '{print $1}')" == "$SOURCE_B2SUM" ]] \
            || die "official source checksum verification failed: $SOURCE_ARCHIVE"
        log "official source checksum verified"
    else
        curl -fL --retry 3 "$signature_url" -o "$signature"
        verify_source_signature "$SOURCE_ARCHIVE" "$signature"
        log "official source signature verified"
    fi

    local i source_patch_url source_patch_file expected_b2
    for i in "${!SOURCE_PATCH_URLS[@]}"; do
        source_patch_url=${SOURCE_PATCH_URLS[$i]}
        expected_b2=${SOURCE_PATCH_B2SUMS[$i]}
        source_patch_file="$downloads/${SOURCE_ID}-prerequisite-$i.patch"
        if [[ ! -f "$source_patch_file" ]] \
           || [[ "$(b2sum "$source_patch_file" | awk '{print $1}')" != "$expected_b2" ]]; then
            log "downloading source prerequisite $((i + 1))/${#SOURCE_PATCH_URLS[@]}"
            curl -fL --retry 3 "$source_patch_url" -o "$source_patch_file.part"
            [[ "$(b2sum "$source_patch_file.part" | awk '{print $1}')" == "$expected_b2" ]] \
                || die "source prerequisite checksum verification failed: $source_patch_url"
            mv "$source_patch_file.part" "$source_patch_file"
        fi
        SOURCE_PATCH_FILES+=("$source_patch_file")
    done
}

validate_patch_paths() {
    local path count=0
    while IFS= read -r path; do
        path=${path%%$'\t'*}
        path=${path#b/}
        ((count += 1))
        case "$path" in
            arch/x86/kvm/*|arch/x86/include/asm/kvm*|arch/x86/include/uapi/asm/kvm*|include/linux/kvm_host.h) ;;
            *) die "patch touches unsupported path: $path" ;;
        esac
    done < <(sed -n -e 's/^--- a\///p' -e 's/^+++ b\///p' "$PATCH_FILE")
    ((count > 0)) || die "patch contains no supported file changes"
}

apply_to_build_tree() {
    # Do not let git discover and inherit a parent repository's ignore rules.
    GIT_CEILING_DIRECTORIES="$WORK_ROOT" git -C "$BUILD_DIR" apply "$@"
}

build_tree_patch_is_applied() {
    local patch=$1

    apply_to_build_tree --check --reverse "$patch" >/dev/null 2>&1 &&
        ! apply_to_build_tree --check "$patch" >/dev/null 2>&1
}

prepare_build_tree() {
    local patch_sha=$1 marker="$BUILD_DIR/.ovo-prepared"
    if ((REBUILD)) && [[ -d "$BUILD_DIR" ]]; then
        case "$(realpath -m "$BUILD_DIR")" in
            "$(realpath -m "$WORK_ROOT")"/builds/*) rm -rf --one-file-system "$BUILD_DIR" ;;
            *) die "refusing to reset path outside work root: $BUILD_DIR" ;;
        esac
    fi
    if [[ -f "$marker" ]] \
       && grep -Fx "build_format=$BUILD_FORMAT" "$marker" >/dev/null \
       && grep -Fx "build_toolchain=$BUILD_TOOLCHAIN" "$marker" >/dev/null \
       && grep -Fx "patch_sha256=$patch_sha" "$marker" >/dev/null; then
        build_tree_patch_is_applied "$PATCH_FILE" \
            || die "cached build tree has an unexpected patch state; rerun with --rebuild"
        [[ -s "$BUILD_DIR/.ovo-kernel.Module.symvers" ]] \
            || die "cached build tree is incomplete; rerun with --rebuild"
        return
    fi
    [[ ! -e "$BUILD_DIR" ]] || die "incomplete cached build tree exists; rerun with --rebuild"

    local extract_dir="${BUILD_DIR}.extracting.$$"
    mkdir -p "$extract_dir"
    log "extracting source into $BUILD_DIR"
    bsdtar -xf "$SOURCE_ARCHIVE" -C "$extract_dir"
    [[ -d "$extract_dir/$SOURCE_ID" ]] || die "unexpected source archive layout"
    mkdir -p "$(dirname "$BUILD_DIR")"
    mv "$extract_dir/$SOURCE_ID" "$BUILD_DIR"
    rmdir "$extract_dir"

    local source_patch
    for source_patch in "${SOURCE_PATCH_FILES[@]}"; do
        apply_to_build_tree --check "$source_patch" \
            || die "source prerequisite does not apply cleanly: $source_patch"
        if apply_to_build_tree --check --reverse "$source_patch" >/dev/null 2>&1; then
            die "source prerequisite has an ambiguous pre-apply state: $source_patch"
        fi
        apply_to_build_tree "$source_patch"
        build_tree_patch_is_applied "$source_patch" \
            || die "source prerequisite post-apply verification failed: $source_patch"
    done

    cp -a "$HEADER_TREE/.config" "$BUILD_DIR/.config"
    local localversion_file
    for localversion_file in "$HEADER_TREE"/localversion.*; do
        [[ -e "$localversion_file" ]] || continue
        cp -a "$localversion_file" "$BUILD_DIR/"
    done
    validate_patch_paths
    apply_to_build_tree --check "$PATCH_FILE" \
        || die "OVO patch does not apply cleanly to $SOURCE_ID"
    if apply_to_build_tree --check --reverse "$PATCH_FILE" >/dev/null 2>&1; then
        die "OVO patch has an ambiguous pre-apply state"
    fi
    apply_to_build_tree "$PATCH_FILE"
    build_tree_patch_is_applied "$PATCH_FILE" \
        || die "post-apply patch verification failed"

    "$BUILD_DIR/scripts/config" --file "$BUILD_DIR/.config" --disable MODULE_SIG_ALL
    make -C "$BUILD_DIR" "${KBUILD_MAKE_ARGS[@]}" olddefconfig prepare modules_prepare
    cp -a "$HEADER_TREE/Module.symvers" "$BUILD_DIR/.ovo-kernel.Module.symvers"
    cp -a "$HEADER_TREE/vmlinux" "$BUILD_DIR/vmlinux"
    cp -a "$HEADER_TREE/System.map" "$BUILD_DIR/System.map"
    [[ "$(make -s -C "$BUILD_DIR" "${KBUILD_MAKE_ARGS[@]}" kernelrelease)" == "$KERNEL_RELEASE" ]] \
        || die "prepared source has the wrong kernel release"
    {
        printf 'build_format=%s\n' "$BUILD_FORMAT"
        printf 'build_toolchain=%s\n' "$BUILD_TOOLCHAIN"
        printf 'kernel_release=%s\n' "$KERNEL_RELEASE"
        printf 'package_version=%s\n' "$KERNEL_PACKAGE_VERSION"
        printf 'cpu_vendor=%s\n' "$KVM_VENDOR"
        printf 'patch_sha256=%s\n' "$patch_sha"
    } > "$marker"
}

verify_module() {
    local file=$1 expected=$2 vermagic
    [[ -s "$file" ]] || die "missing module: $file"
    [[ "$(modinfo -F name "$file")" == "$expected" ]] || die "unexpected module name in $file"
    vermagic=$(modinfo -F vermagic "$file")
    [[ "$vermagic" == "$KERNEL_RELEASE"* ]] || die "wrong vermagic in $file: $vermagic"
}

build_modules() {
    local base_symvers="$BUILD_DIR/.ovo-base.Module.symvers"
    local modpost_args=(-M -i "$base_symvers" -o Module.symvers -T modules.order)

    log "building focused KVM objects with $BUILD_TOOLCHAIN (-j$JOBS) [$KVM_KO]"
    make -C "$BUILD_DIR" -j"$JOBS" "${KBUILD_MAKE_ARGS[@]}" \
        arch/x86/kvm/kvm.o arch/x86/kvm/kvm.mod \
        "arch/x86/kvm/${KVM_KO}.o" "arch/x86/kvm/${KVM_KO}.mod"

    awk -v intel='arch/x86/kvm/kvm-intel' -v amd='arch/x86/kvm/kvm-amd' \
        '$3 != "arch/x86/kvm/kvm" && $3 != intel && $3 != amd' \
        "$BUILD_DIR/.ovo-kernel.Module.symvers" > "$base_symvers"
    {
        printf '%s\n' 'arch/x86/kvm/kvm.o'
        printf '%s\n' "arch/x86/kvm/${KVM_KO}.o"
    } > "$BUILD_DIR/modules.order"

    grep -qx 'CONFIG_MODVERSIONS=y' "$BUILD_DIR/include/config/auto.conf" \
        && modpost_args+=(-m)
    grep -qx 'CONFIG_BASIC_MODVERSIONS=y' "$BUILD_DIR/include/config/auto.conf" \
        && modpost_args+=(-b)
    grep -qx 'CONFIG_EXTENDED_MODVERSIONS=y' "$BUILD_DIR/include/config/auto.conf" \
        && modpost_args+=(-x)
    grep -qx 'CONFIG_MODULE_SRCVERSION_ALL=y' "$BUILD_DIR/include/config/auto.conf" \
        && modpost_args+=(-a)
    grep -qx 'CONFIG_SECTION_MISMATCH_WARN_ONLY=y' "$BUILD_DIR/include/config/auto.conf" \
        || modpost_args+=(-E)
    grep -qx 'CONFIG_MODULE_ALLOW_MISSING_NAMESPACE_IMPORTS=y' "$BUILD_DIR/include/config/auto.conf" \
        && modpost_args+=(-N)

    log "resolving module ABI symbols against the installed kernel"
    (
        cd "$BUILD_DIR"
        ./scripts/mod/modpost "${modpost_args[@]}"
        make --no-print-directory O=. "${KBUILD_MAKE_ARGS[@]}" \
            "OVO_MAKE_ARGS=$KBUILD_MAKE_ARGS_TEXT" \
            -f Makefile -f "$SCRIPT_DIR/kvm/ovo-modfinal.mk" \
            ovo_modfinal
    )
    verify_module "$BUILD_DIR/arch/x86/kvm/kvm.ko" kvm
    verify_module "$BUILD_DIR/arch/x86/kvm/${KVM_KO}.ko" "$KVM_MODINFO_NAME"
    if grep -F 'dbg_rl [%i]' "$PATCH_FILE" >/dev/null; then
        strings "$BUILD_DIR/arch/x86/kvm/kvm.ko" | grep -F 'dbg_rl [%i]' >/dev/null \
            || die "OVO debug marker is missing from the built module"
    fi
}

make_stage() {
    local patch_sha=$1 stamp stage
    stamp=$(date '+%Y%m%d-%H%M%S')
    stage="$WORK_ROOT/stages/${KERNEL_RELEASE}-${patch_sha:0:12}-${stamp}"
    mkdir -p "$stage"
    install -m 0644 "$BUILD_DIR/arch/x86/kvm/kvm.ko" "$stage/kvm.ko"
    install -m 0644 "$BUILD_DIR/arch/x86/kvm/${KVM_KO}.ko" "$stage/${KVM_KO}.ko"
    [[ -n "$(modinfo -F signer "$stage/kvm.ko")" ]] || "$BUILD_STRIP" --strip-debug "$stage/kvm.ko"
    [[ -n "$(modinfo -F signer "$stage/${KVM_KO}.ko")" ]] || "$BUILD_STRIP" --strip-debug "$stage/${KVM_KO}.ko"
    verify_module "$stage/kvm.ko" kvm
    verify_module "$stage/${KVM_KO}.ko" "$KVM_MODINFO_NAME"
    zstd -q -f -T0 -19 "$stage/kvm.ko" -o "$stage/kvm.ko.zst"
    zstd -q -f -T0 -19 "$stage/${KVM_KO}.ko" -o "$stage/${KVM_KO}.ko.zst"
    rm -f "$stage/kvm.ko" "$stage/${KVM_KO}.ko"
    (cd "$stage" && sha256sum kvm.ko.zst "${KVM_KO}.ko.zst" > SHA256SUMS)
    {
        printf 'kernel_release=%s\n' "$KERNEL_RELEASE"
        printf 'kernel_package=%s\n' "$KERNEL_PACKAGE"
        printf 'package_version=%s\n' "$KERNEL_PACKAGE_VERSION"
        printf 'cpu_vendor=%s\n' "$KVM_VENDOR"
        printf 'kvm_ko=%s\n' "$KVM_KO"
        printf 'patch_sha256=%s\n' "$patch_sha"
        printf 'source_id=%s\n' "$SOURCE_ID"
        printf 'build_toolchain=%s\n' "$BUILD_TOOLCHAIN"
        printf 'compiler=%s\n' "$("$BUILD_COMPILER" --version | head -n1)"
        printf 'built_at=%s\n' "$(date --iso-8601=seconds)"
    } > "$stage/METADATA"
    printf '%s\n' "$stage"
}

INSTALL_ACTIVE=0
INSTALL_BACKUP=""
install_error_handler() {
    local status=$?
    trap - ERR
    if ((INSTALL_ACTIVE)); then
        set +e
        log "installation failed; restoring $INSTALL_BACKUP"
        restore_backup "$INSTALL_BACKUP"
        log "previous modules restored"
    fi
    exit "$status"
}
trap install_error_handler ERR

install_stage() {
    local stage=$1 stamp backup sums meta
    (cd "$stage" && sha256sum -c SHA256SUMS >/dev/null) \
        || die "stage checksum verification failed"
    verify_module "$stage/kvm.ko.zst" kvm
    verify_module "$stage/${KVM_KO}.ko.zst" "$KVM_MODINFO_NAME"
    if ((ASSUME_YES == 0)); then
        read -r -p "Install OVO KVM modules for $KERNEL_RELEASE ($KVM_VENDOR)? [y/N] " answer
        [[ "$answer" =~ ^[Yy]$ ]] || { log "installation cancelled; stage retained at $stage"; return; }
    fi
    root_run true
    check_idle
    [[ -f "$MODULE_DIR/kvm.ko.zst" && -f "$MODULE_DIR/${KVM_KO}.ko.zst" ]] \
        || die "installed KVM modules were not found"

    stamp=$(date '+%Y%m%d-%H%M%S')
    backup="$BACKUP_ROOT/${KERNEL_PACKAGE_VERSION}-${KVM_VENDOR}-${stamp}"
    sums=$(mktemp)
    meta=$(mktemp)
    root_run install -d -m 0755 "$BACKUP_ROOT" "$backup"
    root_run install -m 0644 "$MODULE_DIR/kvm.ko.zst" "$backup/kvm.ko.zst"
    root_run install -m 0644 "$MODULE_DIR/${KVM_KO}.ko.zst" "$backup/${KVM_KO}.ko.zst"
    (cd "$MODULE_DIR" && sha256sum kvm.ko.zst "${KVM_KO}.ko.zst" > "$sums")
    {
        printf 'kernel_release=%s\n' "$KERNEL_RELEASE"
        printf 'package_version=%s\n' "$KERNEL_PACKAGE_VERSION"
        printf 'cpu_vendor=%s\n' "$KVM_VENDOR"
        printf 'backed_up_at=%s\n' "$(date --iso-8601=seconds)"
    } > "$meta"
    root_run install -m 0644 "$sums" "$backup/SHA256SUMS"
    root_run install -m 0644 "$meta" "$backup/METADATA"
    rm -f "$sums" "$meta"

    INSTALL_BACKUP=$backup
    INSTALL_ACTIVE=1
    root_run install -m 0644 "$stage/kvm.ko.zst" "$MODULE_DIR/.kvm.ko.zst.new"
    root_run install -m 0644 "$stage/${KVM_KO}.ko.zst" "$MODULE_DIR/.${KVM_KO}.ko.zst.new"
    root_run mv -f "$MODULE_DIR/.kvm.ko.zst.new" "$MODULE_DIR/kvm.ko.zst"
    root_run mv -f "$MODULE_DIR/.${KVM_KO}.ko.zst.new" "$MODULE_DIR/${KVM_KO}.ko.zst"
    root_run depmod "$KERNEL_RELEASE"
    if initramfs_contains_kvm; then root_run mkinitcpio -P; fi
    printf '%s\n' "$backup" | root_run tee "$(last_backup_path)" >/dev/null
    INSTALL_ACTIVE=0
    log "installation complete"
    log "stage: $stage"
    log "backup: $backup"
    log "reboot to load the new modules"
}

if [[ "$ACTION" == restore ]]; then
    rollback
    exit 0
fi

detect_kernel_package
detect_cpu_vendor
select_patch_file
install_dependencies
acquire_lock
check_environment
obtain_source_archive
PATCH_SHA=$(sha256sum "$PATCH_FILE" | awk '{print $1}')
BUILD_DIR="$WORK_ROOT/builds/${KERNEL_RELEASE}-${PATCH_SHA:0:12}/$SOURCE_ID"
prepare_build_tree "$PATCH_SHA"
build_modules
STAGE_DIR=$(make_stage "$PATCH_SHA")
log "stage created: $STAGE_DIR"
if ((BUILD_ONLY)); then
    log "build-only complete; no system modules changed"
else
    install_stage "$STAGE_DIR"
fi
