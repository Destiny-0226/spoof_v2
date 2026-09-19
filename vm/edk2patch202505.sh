#!/usr/bin/env bash

if [ "$EUID" != 0 ]; then
    faillock --reset
    sudo -E "$0" "$@"
    exit $?
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOURCE_DIR="${KVMSPOOF_RESOURCE_DIR:-$SCRIPT_DIR}"
if [[ -z "${KVMSPOOF_WORK_DIR:-}" ]]; then
    echo "错误：未设置 KVMSPOOF_WORK_DIR，拒绝克隆 EDK2 到临时目录。"
    exit 1
fi
WORK_DIR="$KVMSPOOF_WORK_DIR"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

get_distro_id() {
  if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    echo "${ID,,}"
  else
    echo "unknown"
  fi
}

install_debian_dependencies_if_needed() {
  case "$(get_distro_id)" in
    ubuntu|debian|linuxmint|pop)
      echo "正在安装缺失的 EDK2 依赖包（不执行 apt update）..."
      export DEBIAN_FRONTEND=noninteractive
      sudo apt-get install -y \
        acpica-tools build-essential libfdt-dev libglib2.0-dev libpixman-1-dev \
        ninja-build python3-venv zlib1g-dev gnupg python3-sphinx \
        python3-sphinx-rtd-theme patch curl libspice-server-dev \
        libusb-1.0-0-dev libusbredirhost-dev libusbredirparser-dev uuid-dev \
        git nasm python-is-python3 python3-virt-firmware qemu-utils || {
          echo "Error: EDK2 依赖安装失败；未自动执行 apt update，请手动修复软件源缓存后重试。"
          exit 1
        }
      ;;
    *)
      echo "跳过子脚本依赖安装；请确认已通过主脚本或系统包管理器安装 EDK2 编译依赖。"
      ;;
  esac
}

if [[ "${KVMSPOOF_SKIP_SUBSCRIPT_DEP_INSTALL:-0}" != "1" ]]; then
  install_debian_dependencies_if_needed
fi

EDK2_DEST="/usr/local/bin"
CODE_FILE="OVMF_CODE.qcow2"
VARS_FILE="OVMF_VARS.empty.qcow2"
VARS_FILE_2="OVMF_VARS.qcow2"
CODE_DEST="${EDK2_DEST}/${CODE_FILE}"
VARS_DEST="${EDK2_DEST}/${VARS_FILE}"
VARS_DEST_2="${EDK2_DEST}/${VARS_FILE_2}"

resolve_qemu_img() {
  local candidate=""
  local candidates=(
    "${QEMU_IMG_BIN:-}"
    "${QEMU_IMG:-}"
    "$WORK_DIR/qemu/build/qemu-img"
  )

  if command -v qemu-img >/dev/null 2>&1; then
    candidates+=("$(command -v qemu-img)")
  fi

  candidates+=("/usr/bin/qemu-img" "/usr/local/bin/qemu-img")

  for candidate in "${candidates[@]}"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "Error: qemu-img not found. Install qemu-utils, or set QEMU_IMG_BIN=/path/to/qemu-img." >&2
  return 1
}

numbers=(
  "01" "02" "03" "04" "05" "06" "07" "08" "09" "10" "11" "12"
)

get_random_element() {
  local array=("$@")
  echo "${array[RANDOM % ${#array[@]}]}"
}

get_random_string() { head /dev/urandom | tr -dc 'A-Z'    | head -c "$1"; }
get_random_serial() { head /dev/urandom | tr -dc 'A-Z0-9' | head -c "$1"; }
get_random_dec()    { head /dev/urandom | tr -dc '0-9'    | head -c "$1"; }
get_random_hex()    { head /dev/urandom | tr -dc '0-9A-F' | head -c "$1"; }

get_new_string() {
  local random_string=""
  local vowel_count=0
  while [ $vowel_count -ne $2 ]
  do
    random_string="$(get_random_string 100)"
    new_string=$(echo $random_string | sed -E 's/(.)\1+/\1/g' | head -c $1)
    vowel_count=$(echo $new_string | grep -io '[aeiou]' | wc -l)
  done
  prefix=$(echo $new_string | head -c 1)
  suffix=$(echo $new_string | tail -c ${#new_string} | tr '[A-Z]' '[a-z]')
}


if [[ -d edk2backup && ! -f edk2backup/edksetup.sh ]]; then
  echo -e "$(pwd)/\e[1medk2backup\e[0m is incomplete, recloning..."
  rm -rf edk2backup
fi

if [[ ! -d edk2backup ]]; then
  echo -e "$(pwd)/\e[1medk2backup\e[0m does not exist, clone started..."
  git clone --recursive --single-branch --branch edk2-stable202505 https://github.com/tianocore/edk2.git edk2backup
else
  echo -e "$(pwd)/\e[1medk2backup\e[0m found."
  sudo chmod -R 777 edk2backup
fi

file_MdeModulePkg="$(pwd)/edk2/MdeModulePkg/MdeModulePkg.dec"
file_Dsdt="$(pwd)/edk2/OvmfPkg/Bhyve/AcpiTables/Dsdt.asl"
file_Facp="$(pwd)/edk2/OvmfPkg/Bhyve/AcpiTables/Facp.aslc"
file_Hpet="$(pwd)/edk2/OvmfPkg/Bhyve/AcpiTables/Hpet.aslc"
file_Madt="$(pwd)/edk2/OvmfPkg/Bhyve/AcpiTables/Madt.aslc"
file_Mcfg="$(pwd)/edk2/OvmfPkg/Bhyve/AcpiTables/Mcfg.aslc"
file_Platform="$(pwd)/edk2/OvmfPkg/Bhyve/AcpiTables/Platform.h"
file_Spcr="$(pwd)/edk2/OvmfPkg/Bhyve/AcpiTables/Spcr.aslc"
file_VbeShim="$(pwd)/edk2/OvmfPkg/Bhyve/BhyveRfbDxe/VbeShim.c"
file_BhyveX64="$(pwd)/edk2/OvmfPkg/Bhyve/BhyveX64.dsc"
file_BhyveSmbiosPlatformDxe="$(pwd)/edk2/OvmfPkg/Bhyve/SmbiosPlatformDxe/SmbiosPlatformDxe.c"
file_SmbiosPlatformDxe="$(pwd)/edk2/OvmfPkg/SmbiosPlatformDxe/SmbiosPlatformDxe.c"
file_QemuFwCfgCacheInit="$(pwd)/edk2/OvmfPkg/Library/QemuFwCfgLib/QemuFwCfgCacheInit.c"
file_FwBlockService="$(pwd)/edk2/OvmfPkg/QemuFlashFvbServicesRuntimeDxe/FwBlockService.c"
file_QemuFlash="$(pwd)/edk2/OvmfPkg/QemuFlashFvbServicesRuntimeDxe/QemuFlash.c"
file_ComponentName="$(pwd)/edk2/OvmfPkg/QemuVideoDxe/ComponentName.c"
file_Driver="$(pwd)/edk2/OvmfPkg/QemuVideoDxe/Driver.c"
file_ShellPkg="$(pwd)/edk2/ShellPkg/ShellPkg.dec"
file_QemuBootOrderLib="$(pwd)/edk2/OvmfPkg/Library/QemuBootOrderLib/QemuBootOrderLib.c"
file_AuthServiceInternal="$(pwd)/edk2/SecurityPkg/Library/AuthVariableLib/AuthServiceInternal.h"

if [[ -f "$file_MdeModulePkg" ]]; then rm "$file_MdeModulePkg"; fi
if [[ -f "$file_Dsdt" ]]; then rm "$file_Dsdt"; fi
if [[ -f "$file_Facp" ]]; then rm "$file_Facp"; fi
if [[ -f "$file_Hpet" ]]; then rm "$file_Hpet"; fi
if [[ -f "$file_Madt" ]]; then rm "$file_Madt"; fi
if [[ -f "$file_Mcfg" ]]; then rm "$file_Mcfg"; fi
if [[ -f "$file_Platform" ]]; then rm "$file_Platform"; fi
if [[ -f "$file_Spcr" ]]; then rm "$file_Spcr"; fi
if [[ -f "$file_VbeShim" ]]; then rm "$file_VbeShim"; fi
if [[ -f "$file_BhyveX64" ]]; then rm "$file_BhyveX64"; fi
if [[ -f "$file_BhyveSmbiosPlatformDxe" ]]; then rm "$file_BhyveSmbiosPlatformDxe"; fi
if [[ -f "$file_SmbiosPlatformDxe" ]]; then rm "$file_SmbiosPlatformDxe"; fi
if [[ -f "$file_QemuFwCfgCacheInit" ]]; then rm "$file_QemuFwCfgCacheInit"; fi
if [[ -f "$file_FwBlockService" ]]; then rm "$file_FwBlockService"; fi
if [[ -f "$file_QemuFlash" ]]; then rm "$file_QemuFlash"; fi
if [[ -f "$file_ComponentName" ]]; then rm "$file_ComponentName"; fi
if [[ -f "$file_Driver" ]]; then rm "$file_Driver"; fi
if [[ -f "$file_ShellPkg" ]]; then rm "$file_ShellPkg"; fi
if [[ -f "$file_QemuBootOrderLib" ]]; then rm "$file_QemuBootOrderLib"; fi
if [[ -f "$file_AuthServiceInternal" ]]; then rm "$file_AuthServiceInternal"; fi
mkdir -p edk2
cp -fr edk2backup/. edk2/.
if [[ ! -f edk2/edksetup.sh ]]; then
  echo "错误：EDK2 源码不完整，缺少 edk2/edksetup.sh"
  exit 1
fi
if [[ -f /sys/firmware/acpi/bgrt/image ]]; then
  cp -fr /sys/firmware/acpi/bgrt/image edk2/MdeModulePkg/Logo/Logo.bmp
fi

echo "  $file_MdeModulePkg"
echo "\"EDK II\"                                          -> \"American Megatrends Inc.\""
echo "\"INTEL \"                                          -> \"ALASKA\""
echo "0x20202020324B4445                                -> 0x20202049204D2041" #"    2KDE","   I M A"
sed -i "$file_MdeModulePkg" -Ee "s/\"EDK II\"/\"American Megatrends Inc.\"/"
sed -i "$file_MdeModulePkg" -Ee "s/\"INTEL \"/\"ALASKA\"/"
sed -i "$file_MdeModulePkg" -Ee "s/0x20202020324B4445/0x20202049204D2041/"

echo "  $file_Dsdt"
echo "\"BHYVE\"                                           -> \"ALASKA\""
echo "\"BVDSDT\"                                          -> \"A M I   \""
sed -i "$file_Dsdt" -Ee "s/\"BHYVE\"/\"ALASKA\"/"
sed -i "$file_Dsdt" -Ee "s/\"BVDSDT\"/\"A M I   \"/"

echo "  $file_Facp"
echo "'B','V','F','A','C','P',' ',' '                   -> 'A',' ','M',' ','I',' ',' ',' '"
sed -i "$file_Facp" -Ee "s/'B','V','F','A','C','P',' ',' '/'A',' ','M',' ','I',' ',' ',' '/"

echo "  $file_Hpet"
echo "'B','V','H','P','E','T',' ',' '                   -> 'A',' ','M',' ','I',' ',' ',' '"
sed -i "$file_Hpet" -Ee "s/'B','V','H','P','E','T',' ',' '/'A',' ','M',' ','I',' ',' ',' '/"

echo "  $file_Madt"
echo "'B','V','M','A','D','T',' ',' '                   -> 'A',' ','M',' ','I',' ',' ',' '"
sed -i "$file_Madt" -Ee "s/'B','V','M','A','D','T',' ',' '/'A',' ','M',' ','I',' ',' ',' '/"

echo "  $file_Mcfg"
echo "'B','V','M','C','F','G',' ',' '                   -> 'A',' ','M',' ','I',' ',' ',' '"
sed -i "$file_Mcfg" -Ee "s/'B','V','M','C','F','G',' ',' '/'A',' ','M',' ','I',' ',' ',' '/"

echo "  $file_Platform"
echo "'B','H','Y','V','E',' '                           -> 'A','L','A','S','K','A'"
echo "'B','H','Y','V'                                   -> 'A','M','I',' '"
sed -i "$file_Platform" -Ee "s/'B','H','Y','V','E',' '/'A','L','A','S','K','A'/"
sed -i "$file_Platform" -Ee "s/'B','H','Y','V'/'A','M','I',' '/"

echo "  $file_Spcr"
echo "'B','V','S','P','C','R',' ',' '                   -> 'A',' ','M',' ','I',' ',' ',' '"
sed -i "$file_Spcr" -Ee "s/'B','V','S','P','C','R',' ',' '/'A',' ','M',' ','I',' ',' ',' '/"

echo "  $file_VbeShim"
get_new_string 4 1
echo "\"VESA\"                                            -> \"$new_string\""
sed -i "$file_VbeShim" -Ee "s/\"VESA\"/\"$new_string\"/"
get_new_string 4 1
echo "\"FBSD\"                                            -> \"$new_string\""
sed -i "$file_VbeShim" -e  '/OemNameAddress/{n;d;}'
sed -i "$file_VbeShim" -Ee "/OemNameAddress/a\  CopyMem (Ptr, \"$new_string\", 5);"
#get_new_string 4 1
echo "\"FBSD\"                                            -> \"$new_string\""
sed -i "$file_VbeShim" -e  '/VendorNameAddress/{n;d;}'
sed -i "$file_VbeShim" -Ee "/VendorNameAddress/a\  CopyMem (Ptr, \"$new_string\", 5);"

echo "  $file_BhyveX64"
echo "\"BHYVE\"                                           -> \"ALASKA\""
sed -i "$file_BhyveX64" -Ee "s/\"BHYVE\"/\"ALASKA\"/"

echo "  $file_BhyveSmbiosPlatformDxe"
version_major="$(shuf -i 1-9 -n 1)"
version_minor="$(shuf -i 11-99 -n 1)"
day="$(get_random_element "${numbers[@]}")"
month="$(get_random_element "${numbers[@]}")"
year="$(shuf -i 2015-2025 -n 1)"
echo "\"EFI Development Kit II / OVMF\\0\"                           -> \"American Megatrends Inc.\\0\""
echo "\"0.0.0\\0\"                                                   -> \"$version_major$version_minor\\0\""
echo "\"02/06/2015\\0\"                                              -> \"$day/$month/$year\\0\""
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/\"EFI Development Kit II \/ OVMF\\\\0\"/\"American Megatrends Inc.\\\\0\"/"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/\"0.0.0\\\\0\"/\"$version_major$version_minor\\\\0\"/"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/\"02\/06\/2015\\\\0\"/\"$day\/$month\/$year\\\\0\"/"
echo "0xE800, // UINT16                    BiosSegment            -> 0xE000, // UINT16                    BiosSegment"
echo "0,      // UINT8                     BiosSize               -> 0xFF,   // UINT8                     BiosSize"
echo "1,   // BiosCharacteristicsNotSupported                     -> 0,   // BiosCharacteristicsNotSupported"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/0xE800, \/\/ UINT16                    BiosSegment/0xE000, \/\/ UINT16                    BiosSegment/"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/0,      \/\/ UINT8                     BiosSize/0xFF,   \/\/ UINT8                     BiosSize/"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/1,   \/\/ BiosCharacteristicsNotSupported/0,   \/\/ BiosCharacteristicsNotSupported/"
echo "           // Remaining BiosCharacteristics bits left unset :60"
echo "           v v v v v v v v v v v v v v v v v v v v v v v v v v"
echo "           0    // ReservedForVendor                        :32"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a0x400013   // ReservedForVendor                             :32"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // NecPc98                                       :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // CgaMonoIsSupported                            :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      1,   // PrinterIsSupported                            :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      1,   // SerialIsSupported                             :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // Keyboard8042IsSupported                       :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // PrintScreenIsSupported                        :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // Floppy35_288IsSupported                       :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // Floppy35_720IsSupported                       :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // Floppy525_12IsSupported                       :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // Floppy525_360IsSupported                      :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // JapaneseToshibaFloppyIsSupported              :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // JapaneseNecFloppyIsSupported                  :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      1,   // EDDSpecificationIsSupported                   :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // BootFromPcmciaIsSupported                     :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // RomBiosIsSocketed                             :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      1,   // SelectableBootIsSupported                     :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      1,   // BootFromCdIsSupported                         :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // EscdSupportIsAvailable                        :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // VlVesaIsSupported                             :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      1,   // BiosShadowingAllowed                          :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      1,   // BiosIsUpgradable                              :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // ApmIsSupported                                :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // PlugAndPlayIsSupported                        :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // PcmciaIsSupported                             :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      1,   // PciIsSupported                                :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // EisaIsSupported                               :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // McaIsSupported                                :1"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "/           \/\/ Remaining BiosCharacteristics bits left unset :60/a\      0,   // IsaIsSupported                                :1"
echo "0,   // BiosReserved                                        -> 0x03, // BiosReserved"
echo "0x1C // SystemReserved                                      -> 0x0D // SystemReserved"
echo "0,     // UINT8                     SystemBiosMajorRelease  -> $version_major,     // UINT8                     SystemBiosMajorRelease"
echo "0,     // UINT8                     SystemBiosMinorRelease  -> $version_minor,    // UINT8                     SystemBiosMinorRelease"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/0,   \/\/ BiosReserved/0x03, \/\/ BiosReserved/"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/0x1C \/\/ SystemReserved/0x0D \/\/ SystemReserved/"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/0,     \/\/ UINT8                     SystemBiosMajorRelease/$version_major,     \/\/ UINT8                     SystemBiosMajorRelease/"
sed -i "$file_BhyveSmbiosPlatformDxe" -Ee "s/0,     \/\/ UINT8                     SystemBiosMinorRelease/$version_minor,    \/\/ UINT8                     SystemBiosMinorRelease/"

echo "  $file_SmbiosPlatformDxe"
echo "0xE800, // UINT16                    BiosSegment            -> 0xE000, // UINT16                    BiosSegment"
echo "0,      // UINT8                     BiosSize               -> 0xFF,   // UINT8                     BiosSize"
echo "1,   // BiosCharacteristicsNotSupported                     -> 0,   // BiosCharacteristicsNotSupported"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/0xE800, \/\/ UINT16                    BiosSegment/0xE000, \/\/ UINT16                    BiosSegment/"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/0,      \/\/ UINT8                     BiosSize/0xFF,   \/\/ UINT8                     BiosSize/"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/1,   \/\/ BiosCharacteristicsNotSupported/0,   \/\/ BiosCharacteristicsNotSupported/"
echo "    // Remaining BiosCharacteristics bits left unset :60"
echo "    v v v v v v v v v v v v v v v v v v v v v v v v v v"
echo "    0    // ReservedForVendor                        :32"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a0x400013 // ReservedForVendor                             :32"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // NecPc98                                       :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // CgaMonoIsSupported                            :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    1,   // PrinterIsSupported                            :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    1,   // SerialIsSupported                             :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // Keyboard8042IsSupported                       :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // PrintScreenIsSupported                        :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // Floppy35_288IsSupported                       :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // Floppy35_720IsSupported                       :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // Floppy525_12IsSupported                       :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // Floppy525_360IsSupported                      :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // JapaneseToshibaFloppyIsSupported              :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // JapaneseNecFloppyIsSupported                  :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    1,   // EDDSpecificationIsSupported                   :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // BootFromPcmciaIsSupported                     :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // RomBiosIsSocketed                             :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    1,   // SelectableBootIsSupported                     :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    1,   // BootFromCdIsSupported                         :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // EscdSupportIsAvailable                        :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // VlVesaIsSupported                             :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    1,   // BiosShadowingAllowed                          :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    1,   // BiosIsUpgradable                              :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // ApmIsSupported                                :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // PlugAndPlayIsSupported                        :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // PcmciaIsSupported                             :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    1,   // PciIsSupported                                :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // EisaIsSupported                               :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // McaIsSupported                                :1"
sed -i "$file_SmbiosPlatformDxe" -Ee "/    \/\/ Remaining BiosCharacteristics bits left unset :60/a\    0,   // IsaIsSupported                                :1"
echo "0,   // BiosReserved                                        -> 0x03, // BiosReserved"
echo "0x1C // SystemReserved                                      -> 0x0D // SystemReserved"
echo "0,     // UINT8                     SystemBiosMajorRelease  -> $version_major,     // UINT8                     SystemBiosMajorRelease"
echo "0,     // UINT8                     SystemBiosMinorRelease  -> $version_minor,    // UINT8                     SystemBiosMinorRelease"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/0,   \/\/ BiosReserved/0x03, \/\/ BiosReserved/"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/0x1C \/\/ SystemReserved/0x0D \/\/ SystemReserved/"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/0,     \/\/ UINT8                     SystemBiosMajorRelease/$version_major,     \/\/ UINT8                     SystemBiosMajorRelease/"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/0,     \/\/ UINT8                     SystemBiosMinorRelease/$version_minor,    \/\/ UINT8                     SystemBiosMinorRelease/"
echo "VendStr = L\"unknown\";                                       -> VendStr = L\"American Megatrends Inc.\";"
echo "VersStr = L\"unknown\";                                       -> VersStr = L\"$version_major$version_minor\";"
echo "DateStr = L\"02/02/2022\";                                    -> DateStr = L\"$day/$month/$year\";"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/VendStr = L\"unknown\";/VendStr = L\"American Megatrends Inc.\";/"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/VersStr = L\"unknown\";/VersStr = L\"$version_major$version_minor\";/"
sed -i "$file_SmbiosPlatformDxe" -Ee "s/DateStr = L\"02\/02\/2022\";/DateStr = L\"$day\/$month\/$year\";/"

echo "  $file_QemuFwCfgCacheInit"
get_new_string 4 1
echo "QEMU FW CFG                                       -> $new_string FW CFG"
sed -i "$file_QemuFwCfgCacheInit" -Ee "s/QEMU FW CFG/$new_string FW CFG/"

echo "  $file_FwBlockService"
#get_new_string 4 1
echo "QEMU flash                                        -> $new_string Flash"
echo "QEMU Flash                                        -> $new_string Flash"
sed -i "$file_FwBlockService" -Ee "s/QEMU flash/$new_string Flash/"
sed -i "$file_FwBlockService" -Ee "s/QEMU Flash/$new_string Flash/"

echo "  $file_QemuFlash"
#get_new_string 4 1
echo "\"QEMU                                             -> \"$new_string"
sed -i "$file_QemuFlash" -Ee "s/\"QEMU/\"$new_string/"
echo "\"QemuFlashDetected                                -> \"${prefix}${suffix}FlashDetected"
sed -i "$file_QemuFlash" -Ee "s/\"QemuFlashDetected/\"${prefix}${suffix}FlashDetected/"

echo "  $file_ComponentName"
#get_new_string 4 1
echo "L\"QEMU                                            -> L\"$new_string"
sed -i "$file_ComponentName" -Ee "s/L\"QEMU/L\"$new_string/"

echo "  $file_Driver"
#get_new_string 4 1
echo "L\"QEMU                                            -> L\"$new_string"
sed -i "$file_Driver" -Ee "s/L\"QEMU/L\"$new_string/"
IFS=':'
cpu_vendor=( $(cat /proc/cpuinfo | grep 'vendor_id' | uniq) )
cpu_vendor="${cpu_vendor[1]}"
if [[ "${cpu_vendor:1}" == "AuthenticAMD" ]]; then
  echo "0x1234                                            -> 0x1022"
  echo "0x1b36                                            -> 0x1022"
  echo "0x1af4                                            -> 0x1022"
  echo "0x15ad                                            -> 0x1022"
  sed -i "$file_Driver" -Ee "s/0x1234/0x1022/"
  sed -i "$file_Driver" -Ee "s/0x1b36/0x1022/"
  sed -i "$file_Driver" -Ee "s/0x1af4/0x1022/"
  sed -i "$file_Driver" -Ee "s/0x15ad/0x1022/"
else
  echo "0x1234                                            -> 0x8086"
  echo "0x1b36                                            -> 0x8086"
  echo "0x1af4                                            -> 0x8086"
  echo "0x15ad                                            -> 0x8086"
  sed -i "$file_Driver" -Ee "s/0x1234/0x8086/"
  sed -i "$file_Driver" -Ee "s/0x1b36/0x8086/"
  sed -i "$file_Driver" -Ee "s/0x1af4/0x8086/"
  sed -i "$file_Driver" -Ee "s/0x15ad/0x8086/"
fi
device=$(( ($(date +"%-d") + $(date +"%-m"))*100 + $(date +"%-d") * $(date +"%-m") ))
echo "0x1111                                            -> 0x$device"
sed -i "$file_Driver" -Ee "s/0x1111/0x$device/"

echo "  $file_ShellPkg"
echo "\"EDK II\"                                          -> \"American Megatrends Inc.\""
sed -i "$file_ShellPkg" -Ee "s/\"EDK II\"/\"American Megatrends Inc.\"/"

echo "  $file_QemuBootOrderLib"
get_new_string $(shuf -i 5-7 -n 1) 3
echo "\"VMMBootOrder%04x\"                                -> \"${prefix}${suffix}%04x\""
sed -i "$file_QemuBootOrderLib" -Ee "s/\"VMMBootOrder%04x\"/\"${prefix}${suffix}%04x\"/"

echo "  $file_AuthServiceInternal"
get_new_string $(shuf -i 5-7 -n 1) 3
echo "L\"certdb\"                                         -> L\"db${prefix}${suffix}\""
sed -i "$file_AuthServiceInternal" -Ee "s/L\"certdb\"/L\"db${prefix}${suffix}\"/"
echo "L\"certdbv\"                                        -> L\"dbv${prefix}${suffix}\""
sed -i "$file_AuthServiceInternal" -Ee "s/L\"certdbv\"/L\"dbv${prefix}${suffix}\"/"

echo "Continue? [auto Y]"

cd edk2
export WORKSPACE="$(pwd)"
export EDK_TOOLS_PATH="${WORKSPACE}/BaseTools"
export CONF_PATH="${WORKSPACE}/Conf"

build_firmware() {
  echo "Building BaseTools (EDK II build tools)..."
  make -C BaseTools; source edksetup.sh
  echo "Compiling OVMF with Secure Boot and TPM support..."
  build \
    -D SECURE_BOOT_ENABLE \
    -D SMM_REQUIRE -D TPM1_ENABLE -D TPM2_ENABLE \
    -a X64 -p OvmfPkg/OvmfPkgX64.dsc \
    -b RELEASE -t GCC5 -n 0 \
    -s -q
}

if [[ -f "$VARS_DEST" ]]; then
  echo -e "${EDK2_DEST}/\e[1m${VARS_FILE}\e[0m found."
  echo "Rebuild? [auto Y]"
  build_firmware || exit 1
else
  build_firmware || exit 1
fi

sudo mkdir -p "$EDK2_DEST"
QEMU_IMG_BIN="$(resolve_qemu_img)" || exit 1
"$QEMU_IMG_BIN" convert -f raw -O qcow2 Build/OvmfX64/RELEASE_GCC5/FV/OVMF_CODE.fd "$CODE_DEST" || exit 1
"$QEMU_IMG_BIN" convert -f raw -O qcow2 Build/OvmfX64/RELEASE_GCC5/FV/OVMF_VARS.fd "$VARS_DEST" || exit 1
echo "$CODE_DEST"
echo "$VARS_DEST"

echo "Set EFI variables? [auto Y]"


cd "$WORK_DIR"
EFI_WORK_DIR="$(pwd)/work"
mkdir -p "$EFI_WORK_DIR"
cd "$EFI_WORK_DIR"

TEMP_JSON="temp.json"
DEFAULTS_JSON="defaults.json"
EFIVARS_DIR="/sys/firmware/efi/efivars"
VARS_LIST=("db" "dbx" "KEK" "PK" "dbDefault" "dbxDefault" "KEKDefault" "PKDefault")
declare -A GUIDS_LIST=(
          ["db"]="d719b2cb-3d3a-4596-a3bc-dad00e67656f"
         ["dbx"]="d719b2cb-3d3a-4596-a3bc-dad00e67656f"
         ["KEK"]="8be4df61-93ca-11d2-aa0d-00e098032b8c"
          ["PK"]="8be4df61-93ca-11d2-aa0d-00e098032b8c"
   ["dbDefault"]="8be4df61-93ca-11d2-aa0d-00e098032b8c"
  ["dbxDefault"]="8be4df61-93ca-11d2-aa0d-00e098032b8c"
  ["KEKDefault"]="8be4df61-93ca-11d2-aa0d-00e098032b8c"
   ["PKDefault"]="8be4df61-93ca-11d2-aa0d-00e098032b8c"
)

{
  printf '%s\n' '{'
  printf '%s\n' '    "version": 2,'
  printf '%s\n' '    "variables": ['
  sep=""
  if [[ -d "$EFIVARS_DIR" ]]; then
    for var in "${VARS_LIST[@]}"; do
      guid="${GUIDS_LIST[$var]}"
      filepath="$EFIVARS_DIR/${var}-${guid}"
      if [[ -f "$filepath" ]]; then
        raw_data=$(sudo hexdump -v -e '1/1 "%.2x"' "$filepath" 2>/dev/null) || raw_data=""
        if [[ ${#raw_data} -ge 8 ]]; then
          attr_hex="${raw_data:6:2}${raw_data:4:2}${raw_data:2:2}${raw_data:0:2}"
          if [[ "$attr_hex" =~ ^[0-9a-fA-F]+$ ]]; then
            attr=$((16#$attr_hex))
          else
            attr=0
          fi
          data_hex="${raw_data:8}"
# Build JSON
          printf '%s\n' "        $sep{"
          printf '            "name": "%s",\n' "$var"
          printf '            "guid": "%s",\n' "$guid"
          printf '            "attr": %d,\n' "$attr"
          printf '            "data": "%s"\n' "$data_hex"
          printf '%s\n' '        }'
          sep=","
        fi
      fi
    done
  fi
  printf '%s\n' '    ]'
  printf '%s\n' '}'
} > "$TEMP_JSON"

if [[ -f "$DEFAULTS_JSON" ]]; then
  echo "Renew EFI variables? [auto Y]"
  cp -f "$TEMP_JSON" "$DEFAULTS_JSON"
  echo "$TEMP_JSON"
else
  cp -f "$TEMP_JSON" "$DEFAULTS_JSON"
  echo "$TEMP_JSON"
fi

virt-fw-vars --input "$VARS_DEST" --output "$VARS_DEST_2" \
  --set-false CustomMode \
  --set-false SecureBootEnable \
  --set-json "$DEFAULTS_JSON" || exit 1
