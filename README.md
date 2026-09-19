# spoof_v2

面向单虚拟机的独立身份、QEMU、OVMF 和 libvirt XML 管理流程。新系统
不读取、不复制、不调用项目外的旧 QEMU/OVMF 补丁或旧 spoof 脚本。

## 目录与产物

```text
resources/qemu11backup/       QEMU 11.0.2 原版源码
resources/ovmfbackup/         edk2-stable202602 原版源码及子模块
resources/battery_ssdt.dsl.in 移动平台电池 SSDT 模板

artifacts/identity-hardware.json  唯一身份/硬件信息源
artifacts/smbios.bin              完整 SMBIOS structure stream

build/qemu/bin/qemu-system-x86_64-ovo
build/qemu/generated/battery.aml  仅电池平台生成
build/ovmf/OVMF_CODE_4M.patched.qcow2
build/ovmf/OVMF_VARS_4M.patched.qcow2
```

`build/` 中还保留每次构建的独立源码副本和 `build-info.json`，原版
`resources/` 不会被打补丁。

## 信息来源与配件池

整机身份不再从机型模板池选择。`01` 直接继承宿主的非唯一 DMI、SMBIOS、
ACPI、CPU 和受支持集成设备信息，因此制造商、产品、主板、BIOS、CPU、
xHCI/HDA 与 PCI subsystem 天然属于同一物理平台。宿主的 UUID、系统/主板/
机箱序列号不会写入产物；脚本只短暂读取这些值用于确认新身份没有意外复制。

`MEMORY_CATALOG`、`STORAGE_CATALOG` 和 `HID_CATALOG` 仅作为可更换配件池保留。
内存容量和磁盘总线来自当前虚拟机 XML；内存代际、速度、外形和插槽命名样式
来自宿主 SMBIOS，再从对应容量池选择同厂商料号。Guest 内存优先避免 4 GiB
模块，其次优先等容量双条，再减少条数：双槽平台上 4/8/12/16/24/32 GiB
分别为 `1x4`、`1x8`、`8+4`、`2x8`、`16+8`、`2x16`。
单槽平台会选择能装下的单条；容量超过平台上限、槽位不够或配件池无法表达时
明确失败，不扩大平台能力。4 GiB 只在必要时使用，不用 `2x4` 代替 `1x8`。
Type 16 保留宿主平台最大容量和槽位数；Type 17 为每个槽位生成一条记录，
已安装容量合计等于 XML，空槽 Size=0，不生成模块厂商、型号或序列号。
Type 19 描述当前 `pc-q35-11.0` 的低/高 RAM 区间，Type 20 将虚拟 DIMM
按顺序分配到这些区间；跨越地址空洞的 DIMM 有多条 Type 20，空槽没有映射。
这是非交错的虚拟内存设备描述，不声称真实双通道交错，也不复制宿主物理地址。
`hardware.memory.slots` 表示 Guest 状态，宿主状态仅留在 `host.smbios_non_unique`。
I226-V 是统一的独立 PCIe 网卡。
缺少必需宿主事实、遇到未实现的内存类型或功能设备形态时直接失败，不生成
另一品牌、另一平台或通用设备 fallback。

内存和完整 SMBIOS 的生成、深度校验都由 `01_generate_identity.py` 内部完成；
`02/03/04` 只校验并消费 `identity-hardware.json` 与 `smbios.bin`，没有额外 Python
模块或第五阶段。当前只支持单内存阵列、1～16 槽、4 GiB 整数倍且不超过
512 GiB 的固定 Guest RAM；`currentMemory` 如存在须等于 `memory`。
热插拔内存、Guest NUMA、自定义 QEMU 内存/机器参数等可能改变地址映射的
配置会明确拒绝，不静默改写 XML。该限制不把 Type 16 平台上限改成 512 GiB。
Type 19 的 RAM 区间与 Q35 内存别名布局一致，并非 OS 可用内存统计；固件和
设备保留空间仍可能使 Guest 的“可用内存”少于已安装容量。
schema 26 旧产物不会被原地升级：需要明确重新运行 01，再重建 02/03，
最后按原流程预览并应用 04。仅更新源码不会修改现有身份、运行时固件或虚拟机。

## 使用顺序

```text
python3 01_generate_identity.py
python3 02_patch_qemu.py
python3 03_patch_ovmf.py
python3 04_apply_spoof.py --dry-run
python3 04_apply_spoof.py
```

`01` 和 `04` 会显示 libvirt 虚拟机列表并提示输入编号；`02` 和 `03`
只读取当前 profile，无交互构建。`04 --dry-run` 仅校验 profile/构建产物
并输出域 XML unified diff，不要求虚拟机关机，不写 `/opt`、不创建网络、
不备份、不执行 `virsh define`。

1. `01` 读取选中域的 XML、宿主 CPU、DMI、SMBIOS、DSDT 和集成设备，每次运行都会
   生成新的 UUID、OEM 格式序列号、DIMM/磁盘序列号和 MAC。每块网卡还会
   生成与宿主现有路由不冲突的私有 `/24` 网段、DHCP 固定来宾 IP、
   随机网关 IP，以及带家用路由器厂商 OUI 的网关 LAN MAC。CPU 型号
   与宿主一致，内存容量、vCPU 拓扑和磁盘总线来自域 XML。整机、主板、
   固件版本/日期、ACPI OEM/Creator、PCI subsystem、xHCI、HDA 和电池的
   非唯一规格直接继承宿主；内存/存储/HID 作为兼容配件随机。读取宿主固件表
   通常需要 `sudo`，权限不足、字段缺失或当前后端不能严格承载时立即失败。
2. `02` 验证 QEMU 11.0.2 原版源码，复制后注入完整 SMBIOS 加载器、
   profile 统一的 ACPI OEM/编译器信息、存储标识、USB HID 标识、
   HDA codec 信息、板载 analog 针脚和宿主 OEM PCI subsystem 标识；同时加入独立的
   Intel I226-V PCIe 网卡设备、I225/I226 NVM/PHY 访问层和 2.5GbE 链路状态，并收缩
   FADT、fw_cfg、WAET、AML 调试端口和虚拟化特有 ACPI 命名等暴露面。AHCI 的
   `CAP.ISS`、`PxSSTS.SPD`、ATA major version、UDMA 和 NCQ 由同一 SATA profile
   生成；FADT Preferred PM Profile、C2/C3 latency 来自宿主表，然后构建
   x86_64 KVM-only QEMU。这个产物不包含 TCG 软件模拟回退。构建时将
   QEMU 的内置 data directory 绑定到该 profile 的 `/opt/ovo-spoof/profiles/<id>/qemu`，
   但只暂存到 `build/qemu/`；不会写入 `/opt`，实际安装仍由 `04` 完成。
3. `03` 验证 edk2-stable202602 原版源码及必需子模块，复制后提取宿主
   `/sys/firmware/acpi/bgrt/image` 的 BMP 启动 Logo（没有 BGRT 时保留默认 Logo），再统一
   固件/ACPI 身份、SMBIOS Type 0 兜底、HSTI/SIO/显示组件名称和内部启动变量，构建带 SMM、
   TPM 1.2/2.0 支持的 OVMF。当前策略明确不编译、不开启
   Secure Boot，VARS 也不预注册 PK/KEK/db。
4. `04` 校验 profile ID、profile 哈希、补丁修订号和所有产物哈希，
   通过 `sudo` 一次性部署到 `/opt/ovo-spoof/profiles/<profile_id>/`，备份原 XML，
   再应用 QEMU、OVMF、SMBIOS、CPU/时钟、设备和可选电池
   配置。已有 NVRAM 不会被就地误标为 qcow2；新 profile 使用新的
   qcow2 VARS 目标。部署路径避免 libvirt 运行用户无法穿过用户家目录。
   `04` 会读取当前进程可用的逻辑 CPU，保留有效的原有绑定；没有绑定时自动为每个
   vCPU 生成不重复的 `<vcpupin>`，并把剩余 CPU 分配给 `<emulatorpin>`，不改变磁盘挂载链。
   内存容量、vCPU 数量或拓扑在运行 `01` 后若发生变化，`04` 会拒绝应用，避免
   XML、CPUID/ACPI 和 SMBIOS 相互矛盾；应在最终硬件配额确定后重新运行 `01`。
   现有虚拟网卡会收敛为 `i226-v`，来宾看到固定且一致的 `8086:125c`；集成 GPY PHY 使用 I225/I226 的 MDIO 地址 0，并支持驱动初始化需要的 Clause 22/45 访问。LTR、DMA 合并、扩展唤醒状态和 I225/I226 统计寄存器也由设备模型独立提供；`RPTHC`/`HGPTC` 按真实的读取后清零语义跟踪收发包，避免驱动统计出现全 1 值或计数回绕。
   I226-V、`8086:0000` subsystem、revision 04 和 5.0 GT/s x1 链路；MAC 每次由
   `01` 随机生成，NVM 永久 MAC 与 PCIe Device Serial Number 从同一 MAC
   派生。PCI 能力布局还包含 5 个 MSI-X vector、1 MiB Option ROM BAR、
   AER v2、DSN、LTR、L1 PM Substates 和 PTM requester。该设备不暴露项目名，
   也不通过给 `e1000e` 修改 PCI ID 来实现。
   I226-V 设备层提供 4 组 RX/TX descriptor queue；构建自检覆盖四组队列
   的复位状态以及 MRQC、RETA、RSSRK、RXCSUM、IVAR 和 5 组 EITR。
   libvirt NAT 后端保持单 TAP，就像实体网卡只有一个外部链路；来宾收到的
   数据包仍由 I226-V 内部 RSS/RETA 分配到 descriptor queue，多个发送队列
   最终也汇入同一后端链路。不写入会被 libvirt 对非 virtio 型号静默丢弃的
   `<driver queues='4'>`，避免配置文件声称存在实际并不存在的 4 个 TAP FD。
   Intel Windows `e2fexpress` 驱动的 INF 对 I226-V 默认使用 RSS hash-only
   模式，因此 `Get-NetAdapterRss` 可能仍不列出该网卡；这是驱动策略，
   不通过修改 Windows 注册表伪造。设备寄存器与 descriptor queue 保持完整。
   `04` 会为每块 I226-V 定义身份专属的 libvirt NAT 网络，将网桥 MAC
   设为上述网关 LAN MAC，并通过 DHCP 租约下发来宾 IP、网关和 DNS。
   因此 Windows `arp -a` 中的默认网关不再是 libvirt 默认的 `52:54:00` 地址。
   重新生成身份并应用后，上一个 profile 的专属网络会自动移除。
   libvirt 和 QEMU 统一使用通用 `qemu-xhci` 实现，不再借用
   `nec-xhci`/Renesas 类及其 quirks。受支持的 Intel 宿主使用集成 PCH 能力布局、
   MSI 和 `PCI0.XHCI` ACPI 节点；受支持的 AMD 宿主使用独立 PCIe endpoint、
   MSI-X 和宿主 AMD PCI 身份。当前 Lenovo 82RF 宿主观测值为
   `8086:51ed` revision 01、`17aa:380d` subsystem、`00:14.0`、12 个 USB2 +
   4 个 USB3 端口、64 KiB BAR 和 8-vector MSI；不再对来宾表现为
   Renesas `1033:0194`，也不再使用 Renesas-specific quirks。ACPI 中该
   集成控制器使用 `PCI0.XHCI` 节点，而不是通用槽位名 `PCI0.SA0_`。
   在用的 PCIe Root Port 保持 libvirt 的 `pcie-root-port` 控制器类型，
   但使用 `ioh3420` 后端去掉 Red Hat Generic Root Port ID，并关闭内部
   Root Port 的热插拔，避免 Windows 把 xHCI 等内部设备显示为可弹出硬件；
   多余 Root Port 会被删除。每个保留端口按 `target port` 映射到不同的宿主
   Root Port PCI 身份和最大链路 speed/width，不再让所有端口复用同一 DID。
   如果后续 PCI controller 要求索引连续，中间的空 Root Port 也会保留，并使用
   宿主未连接但有效的 Root Port 身份；不再由 libvirt 在 define 后暗中补齐；
   显式检测到 PCI 显卡直通时会移除虚拟显卡和 SPICE/VNC 显示设备。
   当前持久存储契约明确收敛为一块 SATA SSD；`01` 会拒绝其他持久磁盘总线、
   多系统盘和软盘，不再在 SATA SSD 与机械盘之间随机切换。SSD 型号族与固件等
   非唯一信息从可信 SATA SSD 配件池随机选择，serial 和 WWN 独立随机并由 profile 锁定。
   Guest 的 ATA Identify 会同时得到型号、固件、serial、WWN、非旋转介质、TRIM、
   SATA 代际、ATA major version、UDMA 与 NCQ，避免型号和介质能力互相矛盾。
   持久磁盘的 SATA 目标和挂载链仍严格保留 `01` 读取的当前 XML 设置；
   安装完成后卸载 ISO/CD-ROM 不会再阻断 `04` 重复应用。
   libvirt 的 SATA XML 不接受 WWN、vendor/product 等 SCSI 专用节点；`04` 不写入
   这些无效节点。由于 profile 只允许一块 SATA SSD，`02` 会把随机 WWN 作为该
   profile 专属 `ide-hd` 的默认值写进 QEMU，serial 仍由 XML 逐盘注入。
   `VM Generation ID` 会被删除，不再把虚拟化专用 ACPI 设备当作随机身份。
   板载 HDA 只采集 Intel/AMD 芯片组 analog codec：必须在 PCI 总线 0、厂商
   `8086`/`1022`，且不能是独显/核显的 HDMI 功能。USB 声卡和独立 PCIe 声卡
   不进入身份，留给直通。`01` 写入宿主 BDF 和实际启用的 pin default；没有
   播放针脚的板载 codec 直接失败。`02` 用这些针脚替换 QEMU duplex/output
   的 Line Out/Line In 图，不手搓 Realtek 私有寄存器。`04` 把现有
   libvirt 的 Q35 会把 `00:1f.3` 留给隐式 SMBus，所以 `04` 删除 `<sound>`，
   改用 `qemu:commandline` 在宿主 BDF 挂上 `ich9-intel-hda` + duplex/output
   codec。`<audio>` 后端（如 spice）保持不动。
   DSDT 会公布与实际 i8254 一致的 `PNP0100` 系统定时器，同时保留
   RTC 和 HPET，避免 Windows 出现“有 PIT 实现但无 PIT ACPI 设备”的矛盾。
   未使用显卡直通时仍保留临时 QEMU VGA `1234:1111`（Microsoft Basic Display
   可启动，SPICE/本地查看器才有帧缓冲）。不把宿主 iGPU/独显 ID 写到 stdvga
   上，也不再用 `8086:1111` 这种不存在的 Intel GPU；OEM subsystem 仍继承宿主
   显示设备。USB HID 的 iSerialNumber 只用配件池序列号，不再拼接
   `0000:00:14.0` 这类 xHCI PCI 路径。正式环境交由 GPU 直通替代虚拟显示。
   `04` 删除默认的 QEMU usb-kbd/usb-mouse/tablet/ps2，不添加 USB tablet。
   始终在宿主 PCH 槽位（当前 82RF 为 `00:14.0`）生成涂过身份的 `qemu-xhci`，
   使用宿主 DID（`8086:51ed`），不写 libvirt `ports='15'`，也不把控制器挂到
   Root Port 后面。只有把宿主这颗 PCH xHCI 本身 PCI 直通进来宾时，才改为
   `<controller type='usb' model='none'/>`，避免和直通设备抢同一地址。
   `04` 将 Hyper-V 启蒙全部关掉，并在宿主有 `ibrs`/`spec_ctrl` 时要求
   `spec-ctrl`。MCE bank 数量继承宿主 sysfs；CPU 热插拔 IO 由 profile 锁定
   且 QEMU/OVMF 成对改掉 `0x0CD8`。SMBIOS Type 8/9 原样继承宿主端口和插槽，
   没有就不编。VGA EDID 不涂，等 GPU 直通。

## 源码下载和构建依赖

如果两个源码目录不存在，对应脚本会自动从上游锁定 tag 下载。
QEMU Git 树默认不携带 `keycodemapdb`，`02` 会根据上游 Meson wrap 的
锁定 revision 自动补全。`03` 每次构建前都会同步、初始化并强制复位全部
递归子模块到 `edk2-stable202602` Git 树记录的 commit；源码 tag、顶层文件、
子模块 commit 或工作树不一致时不会进入构建。这些下载操作需要网络。

构建工具会在开始时一次性检查。发现缺失项后，会通过 `pacman`、`apt-get`
或 `dnf` 自动安装；非 root 环境使用 `sudo`，安装后会再次验证：

- QEMU：`git make python3 ninja meson pkg-config cc/gcc`
- OVMF：`git make gcc g++ nasm python3 iasl qemu-img`

身份文件当前 schema 为 27，宿主平台来源版本为 3。修改或重新运行 `01` 后，必须按顺序重新
运行 `02` 和 `03`；本次 QEMU 运行时前缀、OVMF Logo、Host Bridge DID 和南桥槽位对齐也要求
重新运行 `02`、`03`。`04` 会拒绝混用不同 profile、旧运行时前缀或旧 Logo 的产物。

`product_name`（如 Lenovo `82RF`）、`board_name`（如 `LNVNB161216`）、
机型、SKU、BIOS 版本和 ACPI OEM 信息直接继承同一宿主，它们是同批产品共享
的非唯一属性。系统/主板/机箱/内存/磁盘/电池序列号、SMBIOS UUID、MAC 和
NVRAM ID 每次重新生成，并由 profile 锁定供后续脚本统一使用。

南桥相关 PCI 身份也由宿主实时采集并锁定：`host_bridge`、`lpc`、`smbus`、
`sata` 同时记录主 Vendor/Device/Revision、Subsystem 和产品描述。`02` 会将
这些 Guest 可见字段写入 Q35 后端，并把机器描述从默认的 `Q35 + ICH9` 改为宿主
平台值；QEMU 内部的 `TYPE_ICH9_*` 实现名称保留，以避免破坏设备连接和迁移状态。
Host Bridge DID 是单一来源：`01` 写入 `devices.pci_identities.host_bridge`，`02`
改 QEMU `00:00.0`，`03` 同步 OVMF `INTEL_Q35_MCH_DEVICE_ID`。两边必须一致，
否则带 `SMM_REQUIRE` 的 OVMF 会在 PEI 的 `Q35BoardVerification` 死循环，表现为
`guest has not initialized the display (yet)`。

南桥 ACPI 节点名同样来自宿主，而不是写死 `LPCB`。`01` 读取对应 PCI 设备的
`firmware_node/path`（例如 `\_SB_.PC00.LPCB`），把 NameSeg 锁进
`devices.acpi_nodes`。`02` 用这些名字替换 Q35 默认的 `PCI0.SF8` 槽位名，QEMU 根
仍是 `PCI0`。LPC/SMBus 的 guest 可见 DEVFN 也跟宿主 BDF 走，OVMF 的 PMBASE/PIRQ
访问与 LPC 成对对齐；Intel 采到 `00:1f.0` 时等于 Q35 默认，AMD 采到 FCH
`00:14.3` 一类地址时才搬家。宿主若有 SATA 则继承其 ACPI 名和槽位；NVMe-only
宿主的派生 AHCI 不伪造 ACPI 节点，槽位留在 Q35 的 `1f.2`。Intel 与 AMD 均走同一
宿主 PCI/ACPI/槽位采集路径，AMD 缺少 AHCI 控制器时使用对应平台的现代 FCH/AHCI
派生身份。必须在 PCI bus 0，且不得与 xHCI 或其他南桥功能撞车。

## 电池规则

仅当宿主存在内置 System Battery 时，profile 才启用电池。鼠标、耳机等
`scope=Device` 的电池不会触发该规则。电池厂商、型号、设计容量和电压继承
宿主的非唯一规格，序列号由 `01` 重新随机；电量、健康度和充放电状态按宿主
快照生成，不复制其硬件身份。
移动 profile 的同一份 SSDT 还会补全风扇和温控区，避免出现“移动电源配置但没有温控设备”的矛盾。
生成时会快照宿主的 AC/充放电状态、剩余容量、功率和电压；若宿主不提供
某项数据，才在合理范围内生成。ACPI Creator ID/Revision 与 profile BIOS 厂商和日期保持一致。

## 已知边界

### 身份 schema 28

- `01_generate_identity.py` 是身份和 SMBIOS 的唯一生成与深度校验入口。schema 28 在 schema 27 的完整 SMBIOS 契约上新增单 SATA SSD 介质契约；`02/03/04` 不再重建身份。Type 127 使用 OVMF 最终分配的保留句柄 `0xFEFF`。
- 入口统一为 SMBIOS 3.5，Type 17 长度为 92 字节，声明 DRAM、volatile 工作能力及实装易失容量。额定速度、SPD 编码、Rank、电压缺少虚拟模块证据时保持未知；配置速度是 profile 声明值，不是性能测量。
- Type 0 ROM 为实际构建约束的 4 MiB；OVMF 强制 `FD_SIZE_4MB` 并检查原始 CODE+VARS 容量。UEFI/虚拟机位据实设置，EC 修订号未知，不再声称未验证的传统 BIOS 功能。
- 不把宿主汇总缓存直接声明为 Guest 缓存；当前省略 Type 7，Type 4 缓存引用为 FFFF（SMBIOS 2.3+ 的未提供缓存信息）。磁盘模板的 CPU ID 八字节为运行时占位，不是标准“未知”编码；QEMU 在发布给固件前以 Guest CPUID(1).EAX/EDX 填入每个 Type 4，取不到则拒绝启动。CPU 厂商/型号保留 host-passthrough 模板。
- 磁盘模板校验与 Guest 最终表校验分开。\`validate_guest_stream\` 要求独立采集的 Guest CPUID，只允许这八字节的受控变化；不能拿模板哈希直接要求最终表逐字节相等。Guest 缓存实测尚未接入，明确保持未知。
- Type 9 保留平台槽位描述，但使用状态和 PCI 地址标记未知；宿主电压/温度/电流探针（26/28/29）不直接发布为 Guest 已实现传感器。捕获的宿主原始资料仍保留在 JSON。
- XML 的部分启用 vCPU 或逐 CPU 热插拔状态不受固定模型支持，01/04 将拒绝，不悄悄生成全部启用的 CPU 表。
- 必须明确重新运行 01 并重建 QEMU revision 36、OVMF revision 16 后才能应用。schema 27 及既有构建不会自动升级，也不会因为同步源码而改变现有 VM。
- 源码/二进制回归不等于 Guest 启动验证。最终还需核对固件入口、Guest 原始 SMBIOS、CPUID、系统内存和 WMI/dmidecode；不能仅凭字段更多就认定合规。

上述编码依据 [DMTF DSP0134](https://www.dmtf.org/sites/default/files/standards/documents/DSP0134_3.8.0.pdf)；4 MiB 构建选项来自 [EDK2 OVMF](https://github.com/tianocore/edk2/blob/edk2-stable202602/OvmfPkg/OvmfPkgX64.dsc)。

- Q35/ICH9 是 QEMU 实际实现的芯片组，不会只改 PCI ID 就冒充新一代 PCH。Guest
  可见的非唯一身份（CPU 厂商/型号、DMI、ACPI OEM、南桥 PCI ID/槽位/ACPI 名、
  xHCI）一律继承宿主，所以 AMD 宿主上这些字段是 AMD 的；内部仍是 Q35 的 PM
  寄存器布局，访问地址跟宿主 LPC。
- `fw_cfg` 仍是 OVMF 启动必需的固件私有通道；脚本会取消 ACPI 广播，但不会在
  未证明固件可启动的情况下破坏其 I/O 接口。
- 电池 SSDT 是生成时快照，尚不是宿主电源状态的实时透传。
- Secure Boot 仍明确关闭，当前不自动创建 TPM/swtpm；本轮只同步宿主 FADT、S3/S4
  和 C-state latency。
- I226-V 覆盖 PCIe/NVM/MDIC/MMD、四队列高级描述符、RSS、VLAN、
  校验和/TSO 和 MSI/MSI-X 路径；PTM 当前复刻 PCIe requester 配置空间，
  不模拟物理 PTM 报文交换。TSN 调度和完整 PTP 时间戳仍需基于真实设备
  寄存器捕获继续补全。
- I226-V 的 NVM Auto Read/Shadow Valid 状态、GPY PHY MMD 访问完成状态以及
  软件全局复位后的 PHY ID 均保持一致，避免真实 I226 驱动在初始化阶段超时。
- I226-V 的软件复位和 PCIe FLR 会恢复真实的 RX/TX 队列禁用状态，不继承 82576 队列 0
  默认启用的复位值，保证客户机驱动能够继续建立 DMA 描述符环。
