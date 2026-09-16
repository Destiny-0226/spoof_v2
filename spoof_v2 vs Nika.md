# 完整对比分析结论

本轮已按只读方式核对：

- Nika README、QEMU/OVMF/KVM 补丁脚本、SSDT、网卡模型、EDID 脚本及关键辅助文件。
- `spoof_v2` 的四阶段脚本、README、补充比较文档、当前 JSON/SMBIOS/QEMU/OVMF 构建产物和历史 XML 备份。
- README 声明与实际代码、当前生成 profile、当前二进制产物之间的一致性。

**没有修改任何代码或配置。**

---

# 一、总体判断

不能简单得出“V2 已经全面超过 Nika”。

更准确的结论是：

| 维度 | 结论 |
|---|---|
| 身份统一管理 | V2 明显领先 |
| QEMU/OVMF/SMBIOS 配对一致性 | V2 明显领先 |
| 可复现构建、版本锁定、哈希验证 | V2 明显领先 |
| 失败检测、输入验证 | V2 明显领先 |
| PCIe 网卡模型深度 | V2 的 I226-V 明显领先 |
| SMBIOS 排他注入和表间引用 | V2 明显领先 |
| 自动化 XML 部署 | V2 明显领先，但存在事务和过度修改问题 |
| KVM 内核级检测面 | Nika 覆盖，V2 完全没有 |
| EDID、路由器 MAC、GPU UUID 等外围身份面 | Nika 覆盖更广 |
| Secure Boot/TPM 使用路径 | Nika 有实现尝试，V2 当前明确关闭 |
| 平台身份真实性 | 两边均存在问题；V2 更自洽，但仍把 Q35 行为包装成现代宿主平台 |
| 随机化 | Nika 随机很多但经常无事实依据；V2 更克制，但当前配件池太小、平台继承策略不符合你的最新目标 |

核心区别是：

- **Nika 是一个范围很广但一致性较差的“全链路隐匿集合”。**
- **V2 是一个范围较窄但工程质量、一致性和可验证性更高的“profile 驱动构建系统”。**

后续正确方向不是照搬 Nika，而是：

1. 把 Nika 多出来的部分转化成检测面清单。
2. 对每个检测面建立可验证实现。
3. 保持 V2 的单一 profile、严格校验和配对构建原则。
4. 避免重新引入 Nika 那种“只改字符串或 PCI ID，底层行为没有对应”的问题。

---

# 二、项目范围差异

## 2.1 Nika 实际包含的范围

Nika 不只是 QEMU spoof，README 给出的完整链条包括：

1. Linux 发行版和 libvirt 环境配置。
2. QEMU 源码修改。
3. OVMF 源码修改。
4. Linux/KVM 内核修改。
5. 手工修改 libvirt XML。
6. GPU passthrough。
7. evdev 输入。
8. 自定义 RTL8125 网卡。
9. USB 网卡或 VirtIO 网卡替代路径。
10. memflow。
11. EDID 修改。
12. NVIDIA GPU UUID 操作。
13. 路由器 LAN MAC 修改。
14. TPM/Secure Boot 使用说明。
15. 游戏和反作弊环境操作。

因此 Nika 声称的“100% VMAware undetected”并不只依赖 `qemupatch.sh`，而是依赖它要求的整个部署环境。

## 2.2 V2 实际包含的范围

V2 是四阶段架构：

1. `01_generate_identity.py`
   - 采集宿主事实。
   - 生成随机唯一身份。
   - 生成统一 JSON profile。
   - 生成完整 SMBIOS stream。

2. `02_patch_qemu.py`
   - 从锁定的 QEMU 11.0.2 原版树构建。
   - 将 profile 写入 QEMU。
   - 构建 I226-V、xHCI、HDA、ACPI、SMBIOS 等补丁。

3. `03_patch_ovmf.py`
   - 从 `edk2-stable202602` 构建。
   - 同步固件、ACPI、Host Bridge、LPC、CPU hotplug 等身份。

4. `04_apply_spoof.py`
   - 校验 profile 和构建产物。
   - 部署到 `/opt/ovo-spoof/profiles/<profile-id>/`。
   - 修改 libvirt XML。
   - 创建 profile 专属 NAT 网络。

V2 当前不包含：

- KVM 内核补丁。
- EDID spoof。
- GPU UUID 修改。
- 宿主/路由器公网与 LAN 身份管理。
- Secure Boot 启用和密钥生命周期。
- swtpm 状态创建与 XML 部署。
- 完整 GPU passthrough 自动配置。
- Nika 的作弊/memflow 功能。

因此，如果目标是“完整覆盖 Nika 的所有检测面”，V2 目前还没有达到；如果目标是“构建一个可重复、相互一致的 QEMU/OVMF/SMBIOS profile”，V2 已经明显更成熟。

---

# 三、工程结构和可复现性

## 3.1 Nika 的工程问题

Nika 的 QEMU 脚本：

- clone `stable-11.0` 分支，不锁定具体 commit。
- 没有验证 Git 工作树是否干净。
- 没有记录源码 revision。
- 没有构建产物哈希。
- 没有 profile 与产物绑定。
- 大量使用无匹配数量检查的 `sed -i`。
- 补丁失败时可能继续构建。
- 没有 `set -euo pipefail`。
- 直接覆盖 `/usr/local/bin/qemu-system-x86_64` 和 `/usr/local/share/qemu`。
- QEMU、OVMF、内核可能分别来自不同时间点的源码。

`vars.sh` 虽然让一部分随机值在 QEMU 和 OVMF 间复用，但它不是完整身份模型，只保存：

- `device`
- `vendor`
- `xhci`
- `cpu`
- `virtio`
- 两个 bridge ID

大量其他随机字符串仍在每次脚本运行时重新生成，不一定跨组件同步。

## 3.2 V2 的优势

V2 具有：

- 固定 QEMU tag/revision。
- 固定 edk2 tag/revision。
- EDK2 递归子模块验证。
- 原版源码树校验。
- 严格的 `replace_once` / `replace_literal`。
- profile schema version。
- platform source version。
- patch revision。
- profile ID。
- profile SHA256。
- QEMU/OVMF 产物 SHA256。
- QEMU 固定运行时 prefix。
- profile 独立部署目录。
- 完整 SMBIOS 二进制与 JSON 哈希绑定。
- 构建产物与 profile 不匹配时拒绝应用。

当前 profile 是：

- Profile ID：`a32d94f4-06cf-47ba-b733-85e1d67ad961`
- Schema：23
- Platform source version：3
- QEMU patch revision：32
- OVMF patch revision：14
- QEMU：v11.0.2
- OVMF：`edk2-stable202602`

这一层级是 Nika 完全没有的。

---

# 四、身份生成策略对比

## 4.1 Nika 的策略

Nika 的身份来源混合了：

- 手工填写的平台 PCI ID。
- 宿主 CPU vendor/model。
- 固定 Intel Comet Lake 或 AMD Renoir/FCH 值。
- `$RANDOM` 和日期派生数字。
- `/dev/urandom` 随机字符串。
- 小型磁盘/光驱型号池。
- 手工填写的 SMBIOS XML。
- 固定 DDR4、4 DIMM slot、固定 cache。
- 随机 sensor/fan/temperature。

这会产生大量横向矛盾，例如：

- DMI 可能是 HP 笔记本。
- SMBIOS Type 4 可能写 AMD Ryzen。
- QEMU CPU model 可能被改成另一代 Intel 型号。
- 南桥可能固定为 Comet Lake。
- OVMF 又固定成 AMI/ALASKA。
- 内存永远是 DDR4。
- cache 固定为 128 KiB/128 KiB/12 MiB。
- SATA vendor 可能被替换成随机 `vendor` 数值，而不是 Intel/AMD。
- sensor/fan 表并无真实后端。

它的主要目标是消除默认字符串，而不是建立可信的整机配置图。

## 4.2 V2 的策略

V2 将字段分成两类。

### 宿主继承的非唯一平台事实

例如：

- CPU vendor/model/signature。
- cache。
- MCE bank 数量。
- DMI 产品/主板/机箱/BIOS。
- ACPI OEM/Creator/FADT。
- Host Bridge/LPC/SMBus/SATA/xHCI/HDA PCI 身份。
- PCI BDF。
- Root Port 链路能力。
- 内存类型、速度、form factor。
- 电池规格。
- HDA codec pins。

### 新生成的唯一身份

例如：

- SMBIOS UUID。
- system/board/chassis serial。
- DIMM serial。
- disk serial/WWN。
- MAC。
- PCIe DSN。
- NVRAM ID。
- battery serial。
- USB HID serial。

这种划分比 Nika 合理得多。

## 4.3 当前策略与最新目标冲突 （其实这个里是合理的，之前的修改意见是过时的，现在的框架和模式是最新的）

你的最新目标是：

- 移动平台不再直接复制宿主 Lenovo 型号。
- 应从合理的平台池生成完整、互相兼容的 OEM 平台。

但当前 V2 会严格验证 Guest 平台字段必须等于宿主：

- `04_apply_spoof.py:245-268`
- `01_generate_identity.py:2155-2175`

当前 profile 直接继承：

- `LENOVO`
- `82RF`
- `LNVNB161216`
- `Lengion Y9000P IAH7H`
- `J2CN56WW`
- Lenovo ACPI OEM 信息

因此当前 V2 架构与“移动平台合理随机”是直接冲突的，不只是扩充一个列表就能解决。

后续需要把现在的：

```text
host-non-unique -> 直接作为 Guest 平台身份
```

调整为：

```text
host facts -> 约束条件
platform catalog -> 候选整机平台
compatibility rules -> 筛选
selected platform -> Guest 身份
```

不能简单地分别随机厂商、主板、BIOS 和机型。

---

# 五、SMBIOS 对比

## 5.1 Nika 的 SMBIOS

Nika 扩展 QEMU 内置 SMBIOS 生成器，增加或修改：

- Type 4
- Type 7
- Type 8
- Type 9
- Type 16
- Type 17
- Type 19
- Type 20
- Type 26
- Type 27
- Type 28
- Type 32

它有一些值得学习的方向：

- Type 4 关联 Type 7 cache handles。
- 生成 Type 20 映射。
- 表达空 DIMM。
- Type 16 设备数和 Type 17 数量建立关系。
- 补充 sensor/fan/temperature 类型。

但实际内容问题很大：

- L1/L2/L3 cache 固定。
- DDR4 固定。
- DIMM slot 固定为 4。
- 电压固定。
- sensor/fan/temperature 随机生成，没有实体后端。
- Type 17 serial 使用运行时 `rand()`。
- `srand(time(0))` 使同一镜像身份不稳定。
- 部分 SMBIOS 数据与 README 要求用户手工传入的 SMBIOS 又可能重复或冲突。
- Type 4 manufacturer 甚至固定成 `Intel(R) Corporation`，AMD 路径也可能不一致。

## 5.2 V2 的 SMBIOS 架构优势

V2 自己生成完整 structure stream，然后通过新增的：

```text
-smbios full-file=<binary>
```

排他加载。

加载器会检查：

- 表头长度。
- structure 边界。
- 字符串终止。
- singleton type 重复。
- handle 重复。
- Type 127 终止。
- 必需类型。
- 禁止与其他 SMBIOS 来源混用。

这比 Nika 在 QEMU 内部生成并结合多个 `-smbios type=` 的方式可靠很多。

当前生成类型包括：

- 0 / 1 / 2 / 3
- 4 / 7
- 8 / 9
- 11 / 12 / 13
- 16 / 17 / 19 / 20
- 条件性的 22 / 26 / 28 / 29
- 32 / 127

## 5.3 已确认的 V2 SMBIOS 问题

### 问题一：Type 16 Maximum Capacity 写错

`01_generate_identity.py:2092-2102` 使用 Guest 当前安装内存总量：

```python
total_mb = sum(sizes)
capacity_bytes = total_mb * 1024 * 1024
```

写入 Type 16 `Maximum Capacity`。

但 profile 已经采集了宿主 Type 16：

```json
"maximum_capacity_bytes": 34359738368
```

当前 Guest 是 8 GiB，所以生成的 Type 16 最大容量会是 8 GiB，而宿主物理内存数组最大容量是 32 GiB。

这不是简单的风格问题，而是 SMBIOS 字段语义错误。

### 问题二：Type 16 Number Of Memory Devices 写成已安装 Guest DIMM 数 （这一点是我故意这么设计的，为了和给guest的虚拟内存做好分配，所以采用了这个方法。例如我设置8gb给内存，那就默认一根8g,但是允许单根内存最小4gb,但是优先采用8gb的倍数。例如16gb,可以拆成2*8gb）

`01_generate_identity.py:2101`：

```python
len(sizes)
```

当前 Guest 8 GiB 被拆为一条 8 GiB DIMM，所以 Type 16 会写一个 memory device。

但宿主 Type 16 明确记录：

```json
"device_slots": 2
```

`Number Of Memory Devices` 表示该数组的物理设备槽位数量，不等于当前已安装 DIMM 数。

现有 `COMPARISON_WITH_QEMU_FULL_EMULATION.md` 中“P0 内存拓扑已修复”的表述不准确。它只修复了 Guest 容量拆分和 Type 17/20 数量一致性，没有修复 Type 16 的物理语义。

### 问题三：Type 7/Type 4 cache 建模过于简化

当前只生成三个 cache handle：

- L1
- L2
- L3

并让所有 Type 4 指向同一组 handle。

风险包括：

- L1 instruction/data 被合并。
- 多 socket 不适用。
- `cache_l1_kib/cache_l2_kib/cache_l3_kib` 是宿主聚合信息，不一定是单 socket/per-core cache。
- 混合架构 CPU，如当前 i9-12900H 的 P/E core cache sharing，无法用三张简单 Type 7 准确表达。
- Guest vCPU 裁剪后，host cache aggregate 与 Guest CPUID cache topology 可能不一致。

### 问题四：Type 8/9 直接复制格式化 body

当前宿主有：

- Type 8：17 条
- Type 9：5 条

V2 会复制 formatted body 和 strings，只重建外部 handle。

潜在问题：

- Type 9 内部可能包含 segment/bus/device/function。
- slot usage 可能表示宿主当前占用状态。
- 插槽宽度/类型可能与 Guest Root Port 图不一致。
- Type 8 可能公布 Guest 实际并不存在的物理接口。

当前验证只确保“复制结果等于宿主采集结果”，没有验证它们与 Guest PCI/设备拓扑相容。

### 问题五：补充表的“安全复制”仍不足

当前 profile 复制了：

- Type 12
- Type 13
- Type 26
- Type 28

例如：

- `ConfigOptions1/2/3`
- Lenovo 语言表
- 通用 voltage/temperature probe 描述

虽然过滤了明显唯一字符串，但：

- 表内容仍可能是 OEM 专属。
- Type 26/28 不一定有实际传感器后端。
- 未来如果平台改为随机非 Lenovo，这些表不能继续继承 Lenovo 宿主。

## 5.4 应从 Nika 学什么

应该学习：

- 空槽位表达。
- 物理插槽数与已安装 DIMM 分离。
- Type 16/17/19/20 完整关系。
- Type 4 和 Type 7 的 handle 建模意识。

不应学习：

- 固定四槽。
- 固定 DDR4。
- 固定 cache。
- 随机 sensor/fan/temperature。
- 运行时 `rand()` 改变硬件身份。

---

# 六、QEMU 补丁对比

## 6.1 Nika 的补丁方式

Nika 修改范围非常广，涉及：

- ACPI OEM/Creator。
- fw_cfg 名称和签名。
- WAET。
- debug port。
- PCI routing。
- hotplug ACPI 名称。
- PIT。
- FADT。
- LPC/SMBus/SATA/HDA。
- USB/HID/输入设备。
- NVMe/SCSI/IDE。
- VGA/virtio/vendor ID。
- SMBIOS。
- CPU model 表。
- KVM vendor leaf。
- MCE bank。
- APIC topology。
- 自定义 RTL8125。

问题是很多补丁属于：

- 修改未实例化设备。
- 修改全局 vendor 宏。
- 只替换显示字符串。
- 只改 ID、不改 capabilities/register behavior。
- 对不同设备产生连带影响。

例如它把全局的：

- `PCI_VENDOR_ID_REDHAT`
- `PCI_VENDOR_ID_QEMU`
- `PCI_VENDOR_ID_QUMRANET`
- `PCI_VENDOR_ID_VMWARE`

替换为 Intel 或 AMD。

这不是“只修改当前虚拟机里的一个设备”，而是可能影响同一二进制中的多个设备模型。

## 6.2 V2 的补丁范围更窄但更有针对性

V2 重点修改：

- 完整 SMBIOS loader。
- ACPI OEM/Creator。
- fw_cfg ACPI 隐藏。
- FADT hypervisor ID 清理。
- debug port 删除。
- WAET 删除。
- PIT 发布。
- HPET/MADT 规范化。
- Host Bridge。
- LPC/SMBus/SATA/HDA。
- Root Port。
- xHCI。
- USB HID。
- 存储型号。
- MCE bank。
- CPU hotplug I/O。
- I226-V。
- 临时 VGA。
- 电池 SSDT。

它不追求替换 QEMU 源码里所有 `"QEMU"` 字符串，而是重点处理 Guest 实际可见且会实例化的路径。这比 Nika 更合理。

## 6.3 “二进制中没有 QEMU 字符串”并不成立

当前 QEMU 虽然使用：

```text
--disable-debug-info --enable-strip
```

并且 ELF 没有完整符号表，但 `strings` 仍可找到大量：

- `QEMU`
- `KVM`
- `VirtIO`
- `Bochs`
- `VMware`
- QEMU 源文件路径
- `/ovo_spoof/spoof_v2/build/qemu/source/...`

因此应区分：

1. Guest 正常设备枚举是否能看到默认字符串。
2. 二进制静态字符串是否仍存在。
3. 崩溃日志、错误信息、QMP、设备 help 是否可见。
4. Guest 是否能触发某些包含这些文本的设备接口。

V2 README 中“设备不暴露项目名”对于 I226 的 Guest 设备身份基本成立，但不能扩展理解为“二进制中无 QEMU/OVO 痕迹”。

另外，虽然源码辅助函数和宏包含 `OVO_*`，当前 strip 后没有直接搜到明显 `OVO` 符号，但构建路径仍暴露 `ovo_spoof/spoof_v2`。后续应将“Guest 可见面”和“宿主二进制取证面”分开测试。

---

# 七、Q35、Intel/AMD 平台边界

这是当前最重要的架构问题之一。

## 7.1 Nika 的做法

Nika 无论 Intel/AMD 都使用 Q35。

AMD 时，它会：

- 把 Host Bridge 改成 AMD。
- 把 LPC/SMBus/HDA 改成 AMD。
- 把 LPC/SMBus 搬到类似 `00:14.3` / `00:14.0`。
- 修改 ACPI routing。
- 修改 OVMF 的 LPC 访问地址。
- 修改部分 APIC ID/topology。
- 仍保留 Q35/ICH9 的内部实现。

这只是将 Q35 外观改成 AMD，并没有实现 AMD FCH/Promontory 芯片组。

## 7.2 V2 的做法

V2 对此更加诚实，README 明确承认：

- 实际后端仍是 Q35/ICH9。
- AMD 平台只改变 Guest 可见身份、槽位、ACPI 名称和部分 capability。
- 内部 PM 寄存器布局仍是 Q35。

但当前实现仍存在真实性边界：

- 改 Host Bridge DID 不等于实现对应 Intel Alder Lake Host Bridge。
- 改 LPC/SMBus/SATA ID 不等于实现 Alder Lake PCH。
- AMD ID + Q35 PM/interrupt/register behavior 仍然可能被行为探测区分。
- OVMF 中继续使用 `Q35MchIch9.h`。
- machine type 仍固定 `pc-q35-11.0`。

## 7.3 是否存在可以直接替代 Q35 的 AMD PC chipset

在当前主流 x86 QEMU PC machine 中，没有一个与现代 AMD 桌面/移动 FCH 完整等价、可以直接替代 Q35 并满足 Windows 通用启动的成熟 machine model。

因此后续现实选择是：

### 方案 A：明确支持 Intel-like Q35 profile

- Intel 平台优先。
- 所有南桥设备从同代 Intel 平台 catalog 生成。
- 不宣称 AMD FCH。
- AMD CPU 可以作为 CPU，但主板平台仍建模成 Intel-compatible Q35 OEM 整机。

这在行为一致性上最容易控制。

### 方案 B：继续允许 AMD 外观

但需要明确：

- 这是 AMD identity overlay on Q35。
- 必须建立行为探测清单。
- 不能只验证 PCI ID 和 ACPI 名称。
- 要验证 PMBASE、PIRQ、SMBus、SATA、USB、HPET、IOAPIC、MSI routing 等行为。

### 方案 C：开发真正的 AMD PC chipset model

这是大型 QEMU 设备模型工程，不适合通过当前这种字符串/ID patch 实现。

从真实性和维护成本看，**V2 后续最稳妥的是先把 Intel/Q35 平台做完整，再决定 AMD 是限制、实验性支持，还是投入专门设备模型开发。**

---

# 八、xHCI 对比和 `nec-xhci` 结论

## 8.1 Nika 实际没有使用 `nec-xhci`

对整个 Nika 项目搜索后，没有发现：

- `nec-xhci`
- `PCI_VENDOR_ID_NEC`
- NEC uPD720200 设备绑定

Nika 实际修改的是 QEMU 通用 xHCI/Red Hat 相关 PCI 宏，并按 CPU vendor 区分 Intel/AMD ID。

更严重的是，脚本开头虽然定义了：

```bash
xhci_1022="7914"
xhci_8086="06ED"
```

但真正写入 `PCI_DEVICE_ID_REDHAT_XHCI` 对应宏的却是随机变量：

```bash
xhci=$((49152 - device - ...))
```

相关位置：

- `qemupatch.sh:36`
- `qemupatch.sh:1909`
- `qemupatch.sh:1926`

所以日志中声称改成 AMD `7914` 或 Intel `06ED`，代码实际却写入随机 ID。

这是 README/日志与实际补丁不一致的明确例子。

因此，“Nika 为什么选择 nec-xhci”这个前提不成立。可能是更早版本、其他项目或某个 XML 配置使用过，但当前这份 Nika 源码没有。

## 8.2 V2 的 xHCI 明显更合理

V2 统一使用 `qemu-xhci` 后端，并对平台区分：

### Intel integrated profile

- 使用宿主 Intel ID/subsystem/revision。
- 删除 PCIe endpoint capability。
- MSI。
- PM capability。
- 宿主端口数。
- 宿主 interrupter 数。
- 64 KiB BAR。
- `PCI0.XHCI` ACPI 节点。

### AMD PCIe profile

- 保留 PCIe endpoint capability。
- MSI-X。
- AMD PCI 身份。
- 独立 capability profile。

这是比“借 NEC/Renesas 模型再改 ID”更正确的方向。

## 8.3 V2 的局限

它仍是通用 QEMU xHCI 实现，不是完整的 Intel PCH/AMD xHCI：

- 没有实现各代 PCH vendor-specific registers。
- 没有完整复刻 capability layout。
- PCI config space 的全部 reserved bits/quirks 未对照实机。
- runtime power management 行为未基于实机 trace。
- Intel integrated 和 AMD discrete 的差异目前主要集中在 PCI capability 层。
- 当前 AMD 路径没有实际产物和实机验证。

因此后续不应切换到 `nec-xhci`，而应继续：

1. 保持通用 xHCI core。
2. 将 PCI wrapper 分成 Intel/AMD profile。
3. 用真实设备 config dump、Windows 驱动访问 trace、Linux `lspci -xxxx` 对照完善 wrapper。
4. 为 Intel/AMD 分别建立 qtest。

---

# 九、SATA、SCSI、NVMe、存储身份

## 9.1 Nika

Nika 会修改：

- IDE disk model。
- ATAPI vendor/product。
- SCSI vendor/product。
- NVMe controller 描述。
- SATA PCI ID。
- AHCI/ATA 相关能力。

优点是覆盖面广。

问题是：

- SATA vendor 会被改成随机 `vendor`，不是实际 Intel/AMD。
- 多个存储后端被全局修改。
- 型号、固件、协议能力之间没有统一 profile。
- NVMe 主要是字符串修改，没有看到完整 NVMe controller identity/capability 建模。
- 设备实际是否实例化依赖用户 XML。

## 9.2 V2

V2 已经补齐：

- SATA Gen3 speed。
- AHCI CAP speed。
- NCQ。
- ATA major version。
- UDMA6。
- IDE/SATA/SCSI 型号和 firmware。
- 每设备 serial/WWN。
- XML bus/target 保持。
- 多 SATA/IDE 无法逐设备表达身份时 fail-fast。

这比 Nika 更严谨。

## 9.3 V2 的 NVMe 问题

虽然 `01_generate_identity.py` 有 NVMe catalog，README 也声称支持 NVMe，但 `02_patch_qemu.py` 中没有实际针对 QEMU NVMe controller 的 identity patch。

目前主要行为是：

- XML 保留 NVMe bus。
- profile 生成 NVMe 型号/firmware/serial。
- `04` 不写 libvirt 不支持的 vendor/product/WWN 节点。

这不等于 Guest 一定能看到 profile 中生成的 NVMe model/firmware。

因此当前 README 中“其余使用可逐设备表达身份的 SCSI/NVMe”的表述，需要进一步用真实生成的 QEMU command line 和 Guest NVMe identify data 验证。

应重点核对：

- libvirt 如何将 `<serial>` 映射给 NVMe。
- QEMU NVMe controller vs namespace 的 serial/model/firmware。
- 多 namespace 时身份边界。
- VID/DID/subsystem。
- NVMe Identify Controller 字段。
- PCIe link/capability。
- SMART/log pages。

---

# 十、Root Port 和 PCIe 拓扑

## 10.1 Nika

Nika 主要是：

- 替换 Red Hat vendor/device ID。
- 使用一个基础 Root Port ID。
- 某些路径递增设备 ID。
- 修改 ACPI 名称和 hotplug。
- AMD 时做 APIC/topology hack。

它没有建立“每个 Guest Root Port 对应一个宿主 Root Port profile”的稳定数据结构。

## 10.2 V2

V2 已实现：

- 每个 `guest_target_port` 单独记录身份。
- 每个 port 使用独立 vendor/device/revision/subsystem。
- 独立 max speed/width。
- 使用 `ioh3420` 后端。
- 关闭内部端口 hotplug。
- 删除无用端口。
- 拒绝 XML 中出现未建档 Root Port。

这比 Nika 强很多。

## 10.3 当前问题

### 映射只是按顺序 zip

Guest target port 与宿主 Root Port 的对应，不是按下游设备类别建立。

例如：

- 网卡对应哪个宿主 port。
- xHCI 是集成设备还是 Root Port 下设备。
- GPU 应对应 x16 CPU port。
- NVMe 应对应 x4 port。

目前只是把若干宿主端口身份分配给若干 Guest target port，没有证明拓扑关系合理。

### 使用 max link 而不是实际下游能力

当前 profile 使用最大 speed/width。对空端口问题不大，但如果端口挂载具体设备，还应保证：

```text
Root Port max capability
Endpoint max capability
Current negotiated link
Guest 设备所在 bus
```

四者一致。

### `ioh3420` 行为与 Alder Lake Root Port 仍不等价

即使 DID 改成 Alder Lake，内部仍是 IOH 3420/QEMU Root Port 行为。需要进一步对比：

- PCIe capability version。
- slot capability。
- AER。
- ACS。
- LTR。
- L1SS。
- DPC。
- PTM。
- hotplug capability。
- secondary bus reset。
- PME。

---

# 十一、HDA/声卡

## 11.1 Nika

Nika：

- 把 HDA controller ID 改成 Intel/AMD 平台 ID。
- 把 QEMU codec vendor 改成 Realtek。
- 把 codec ID 改成 ALC1220。
- 没有实现完整 Realtek vendor verbs/private registers。
- 依赖手工 XML/后端配置。

这是典型的“ID 伪装大于行为仿真”。

## 11.2 V2

V2 更谨慎：

- 采集宿主板载 analog HDA controller。
- 排除 GPU HDMI、USB 声卡和独立声卡。
- 采集 codec ID、subsystem、revision。
- 采集实际 pin default。
- 用宿主 pin graph 替换 QEMU duplex/output pin。
- 不宣称实现完整 Realtek private registers。

这一方向比 Nika 更合理。

## 11.3 当前 XML 行为违反你的最新要求

当前 `04_apply_spoof.py`：

- 验证 profile 必须使用 `onboard-hda`。
- 检查现有 `<sound>`。
- 删除现有 `<sound>`。
- 通过 `qemu:commandline` 强制添加：
  - `ich9-intel-hda`
  - `hda-duplex` 或 `hda-output`

关键位置：

- `04_apply_spoof.py:294-295`
- `04_apply_spoof.py:1003-1038`
- `04_apply_spoof.py:1353-1377`

而你的最新设计要求是：

- 不添加用户的 ICH9 声卡。
- 不删除用户的 ICH9 声卡。
- 不根据直通声卡决定 ICH9 去留。
- QEMU 后端仍应保留正确的可选身份适配。

因此后续必须把两层拆开：

### QEMU 构建层

保留：

- HDA controller identity patch。
- codec identity patch。
- pin graph patch。
- 仅当用户实例化相应设备时生效。

### XML 管理层

删除：

- 必须存在 onboard HDA 的 profile 验证。
- 删除 `<sound>`。
- 强制注入 `ich9-intel-hda`。
- 根据 HDA BDF 检测并占位。
- 与直通声卡相关的增删策略。

这是明确的高优先级修改项。

---

# 十二、网卡模型

## 12.1 Nika RTL8125

Nika 的 `rtl8125.c` 不是简单改 e1000e ID，而是自定义设备模型，约 1310 行，包含：

- MMIO。
- PHY/OCP。
- TX/RX descriptor。
- MSI。
- 2.5 Gbps link。
- Realtek 身份。

这部分是 Nika 少数真正进入设备行为层的实现。

但其验证和一致性体系较弱：

- 缺少 profile 绑定。
- 缺少 build-time register tests。
- 缺少 migration/version 管理证据。
- 未证明 Windows Realtek 驱动覆盖的全部寄存器路径。

## 12.2 V2 I226-V

V2 的 I226-V 深度更高：

- 独立 PCIe device。
- NVM。
- MAC/NVM/DSN 一致派生。
- MDIO Clause 22/45。
- GPY PHY。
- 4 RX/TX queues。
- RSS/RETA/RSSRK。
- MSI-X 5 vectors。
- AER v2。
- DSN。
- LTR。
- L1 PM Substates。
- PTM requester。
- Option ROM BAR。
- reset/FLR。
- migration state。
- qtest register contract。

这是 V2 相对 Nika 最明显的技术优势之一。

## 12.3 仍有边界

README 已正确承认：

- PTM 只实现 config capability。
- 没有物理 PTM 报文。
- TSN 未完整。
- PTP 时间戳未完整。

另外还应继续核对：

- EEPROM/NVM 全部 word。
- firmware version 表达。
- PHY link transitions。
- interrupt moderation。
- MSI-X PBA/table semantics。
- power states。
- ASPM/L1SS runtime。
- driver diagnostic paths。
- Windows 驱动版本差异。

---

# 十三、ACPI 对比

## 13.1 Nika

Nika 修改：

- OEM/Creator。
- debug port。
- fw_cfg AML。
- WAET。
- OSYS/_OSI。
- PCI routing。
- CPU/PCI hotplug 名称。
- FADT latency/profile。
- PIT SSDT。
- battery/fan/thermal SSDT。

问题：

- 大量 OEM/Creator 是随机字符串，缺少平台语义。
- SSDT fan/thermal 值是固定或随机值。
- OSYS/_OSI 大段手工插入，未证明和实际 DSDT逻辑兼容。
- PIT SSDT 额外声明 I/O 区域，可能与 QEMU 自身资源声明重复。
- sensor/thermal 并非真实动态后端。
- AMD 路径依旧建立在 Q35 AML 上。

## 13.2 V2

V2：

- ACPI OEM/Creator 与 firmware profile 统一。
- 隐藏 fw_cfg ACPI 节点但保留实际固件通道。
- 删除 debug port。
- 删除 WAET。
- 清 FADT hypervisor vendor ID。
- 从宿主读取 FADT Preferred PM Profile 和 C2/C3 latency。
- 发布真实 QEMU i8254 对应的 PIT `PNP0100`。
- 规范化 HPET/MADT。
- CPU hotplug I/O 与 OVMF同步。
- 条件生成 battery SSDT。

整体更一致。

## 13.3 Battery SSDT 的边界

当前电池、AC、风扇、thermal 值是生成时快照：

- 不随宿主实时更新。
- 不具备真实 EC。
- fan control 不是真实控制。
- thermal thresholds 是静态值。
- Windows 若长期运行，电池状态可能不变化。

因此它适合“启动时合理的移动设备外观”，不等于真实的笔记本 EC/power subsystem。

---

# 十四、OVMF 对比

## 14.1 Nika OVMF

Nika：

- 使用 `edk2-stable202602` branch。
- 没有验证具体 commit 和子模块状态。
- 覆盖启动 Logo。
- 固定：
  - `American Megatrends Inc.`
  - `ALASKA`
  - `A M I`
- 修改 HSTI、video、boot variable、certdb 名称。
- 修改 Host Bridge DID。
- AMD 时修改 LPC 地址。
- 同步 CPU hotplug I/O。
- 编译：
  - Secure Boot
  - SMM
  - TPM1
  - TPM2
- 可以将宿主 EFI 的 PK/KEK/db/dbx 导入 Guest VARS。
- 最后又设置 `SecureBootEnable=false`。

主要问题：

1. 固定 AMI/ALASKA 不一定与 Guest DMI/OEM 平台匹配。
2. 宿主 BGRT 缺失时直接复制命令可能失败。
3. Bhyve 路径修改大多与 QEMU OVMF 无关。
4. 导入宿主 PK/KEK/db/dbx 会复制宿主信任状态。
5. 可能复制宿主特有变量和唯一状态。
6. Secure Boot build、变量导入和最终关闭 Secure Boot 的语义混乱。
7. 没有 build hash/profile binding。
8. 没有 NVRAM 生命周期管理。

## 14.2 V2 OVMF

V2：

- 锁定 edk2 tag/revision。
- 验证顶层工作树。
- 强制同步递归子模块。
- 验证子模块 commit。
- 使用宿主 BGRT，缺失时安全保留默认 Logo。
- 固件 vendor/version/date 与 profile 统一。
- ACPI OEM/Creator 与 QEMU统一。
- Host Bridge DID 与 QEMU统一。
- LPC PMBASE/PIRQ 与 QEMU统一。
- CPU hotplug I/O 与 QEMU统一。
- 清除 VM characteristic bit。
- neutral video/SIO/HSTI/boot variable/fw_cfg event label。
- 生成独立 CODE/VARS template。
- 记录哈希和 build-info。

这一层 V2 明显领先。

## 14.3 Secure Boot/TPM 功能差距

V2 构建：

```text
-D SMM_REQUIRE -D TPM1_ENABLE -D TPM2_ENABLE
```

但没有：

```text
-D SECURE_BOOT_ENABLE
```

并且强制要求：

```json
"secure_boot": false
```

XML 中：

```xml
<loader secure="no">
```

也不会创建 TPM/swtpm。

因此：

- OVMF 中有 TPM driver capability。
- 但没有运行时 TPM device。
- 没有 swtpm state。
- 没有 Secure Boot。
- 没有 per-profile PK/KEK/db/dbx。

这是相对 Nika 的明确功能缺口。

但不应照搬 Nika 的宿主 EFI 变量导入。正确方案应是：

1. 每个 profile 创建独立 swtpm state。
2. 每个 profile 生成独立 PK/KEK。
3. 导入可信的 Microsoft db/dbx，而不是复制宿主整个变量状态。
4. 记录 key fingerprint。
5. CODE、VARS template、运行时 NVRAM、swtpm state 与 profile ID 绑定。
6. XML 中显式配置 TPM 2.0。
7. 完成 Windows `msinfo32`、`Confirm-SecureBootUEFI`、TPM attestation 基础验证。

---

# 十五、KVM 内核层：V2 最大的覆盖缺口

Nika 内核补丁是两项目之间最重要的范围差异。

## 15.1 Nika 修改的检测面

共同部分包括：

- IOAPIC pins 24 → 32。
- IOAPIC version `0x11 → 0x20`。
- LAPIC version 修改。
- MSR 访问行为。
- XCR0 校验。
- VMCALL 行为。
- debug exception。
- VM-exit 快速返回路径。

Intel 路径包括：

- 自定义 CPUID handler。
- 部分 leaf 用 KVM topology。
- 大量 leaf 直接执行宿主 `cpuid`。
- CPUID cache。
- 汇编快速路径。
- INVD/WBINVD/MONITOR/MWAIT 处理。
- RDPMC 不退出。
- debug registers/BTS/DS area。
- 大量 MSR intercept 修改。

AMD 路径包括：

- 初次 CPUID 后清除 CPUID intercept。
- 让 Guest 后续直接执行宿主 CPUID。
- MONITOR/MWAIT 快速路径。
- APERF/MPERF 和 PMU/MSR passthrough。
- RDPMC。
- 大量 AMD MSR 返回或放行。

## 15.2 V2 当前没有覆盖

V2 只做 userspace QEMU/OVMF/XML，因此没有改变：

- CPUID VM-exit latency。
- 异常指令路径。
- RDPMC。
- debug registers。
- KVM MSR intercept。
- IOAPIC/LAPIC version。
- VMCALL。
- MONITOR/MWAIT。
- INVD/WBINVD。
- VM-exit timing。

即使 Guest 的 DMI/PCI/ACPI 看起来一致，低层 timing 和 instruction behavior 仍可能暴露虚拟化。

## 15.3 为什么不能直接复制 Nika 内核补丁

Nika 内核补丁风险很大：

### CPUID 直透可能与 Guest topology 冲突

当前 V2 允许：

- Guest vCPU 数量小于宿主。
- 自定义 sockets/cores/threads。
- 任意 CPU pinning。
- host-passthrough。
- SMBIOS 按 Guest topology 生成。

如果 CPUID topology/cache leaf 直接执行宿主 `cpuid`：

- CPUID 可能报告宿主 14 核/20 线程或 hybrid topology。
- ACPI/MADT 只报告 Guest vCPU。
- SMBIOS Type 4 报告另一套数量。
- Windows processor groups/cache topology 又可能是另一套。

### Hybrid CPU 风险

当前宿主是 i9-12900H，具有 P/E 核异构特征。Nika 的直透策略没有处理：

- 不同 CPU pin 上 CPUID leaf 差异。
- hybrid bit。
- cache sharing。
- core type。
- vCPU migration 到不同宿主核心。

### 稳定性和安全性

直接放开 MSR、RDPMC、debug、MONITOR/MWAIT 等：

- 可能触发宿主硬件状态。
- 可能泄露宿主 PMU/拓扑。
- 可能破坏 migration。
- 可能产生不可预期异常。
- 强绑定内核版本。
- 补丁中存在大量注释掉/试验性代码。

正确做法是把 Nika 的这些点建立为测试矩阵，而不是先照搬补丁：

```text
CPUID result consistency
CPUID latency distribution
MSR success/#GP behavior
RDPMC behavior
APIC/IOAPIC version
VMCALL behavior
debug register behavior
PMU counter availability
MONITOR/MWAIT behavior
INVD/WBINVD behavior
XSETBV/XCR0 behavior
```

然后逐项决定是在：

- libvirt/QEMU 配置层处理；
- KVM 参数层处理；
- 最小内核补丁处理；
- 或明确不支持。

---

# 十六、EDID 和显示

## 16.1 Nika

Nika 有两层 EDID 操作：

1. QEMU EDID generator 中替换：
   - manufacturer。
   - product code。
   - display name。
   - manufacture week/year。

2. Windows `edidpatch.cmd`：
   - 随机 serial。
   - 修改 descriptor 字符串。
   - 输出新的 EDID binary。

`edidpatch.cmd` 没有看到重新计算 EDID block checksum 的逻辑。

它在第 103 行直接拼接修改后的 hex，再输出 `out.bin`，没有对每个 128-byte block 的最后一个 checksum byte重新计算。因此只要修改了 serial 或 descriptor，原 checksum 大概率失效。

这是一个明确的潜在 bug。

## 16.2 V2

V2 明确不 spoof EDID：

- 临时 stdvga 仍为 `1234:1111`。
- 正式环境等待 GPU passthrough。
- EDID 交给真实直通显示链路。

这个边界比随意生成 EDID 更安全。

但如果最终目标确实包含显示器身份管理，应将其设计为独立模块：

- QEMU 虚拟显示 EDID。
- GPU passthrough physical EDID。
- HDMI dummy/capture passthrough EDID。
- Windows registry override。
- 实体显示器 EEPROM。

这些路径不能共用一个简单随机器，而且必须：

- 保持 manufacturer/product/timing 合理。
- 保留 CTA extension。
- 更新每个 128-byte block checksum。
- 保持音频 capabilities 与实际链路一致。

---

# 十七、XML 应用和部署

## 17.1 V2 的优势

`04_apply_spoof.py` 有较完整的验证：

- profile schema。
- profile ID/hash。
- QEMU/OVMF build revision。
- 产物 hash。
- platform source。
- SMBIOS hash。
- VM 名称。
- 内存容量。
- vCPU 数量和 topology。
- storage layout。
- network count。
- PCI hostdev。
- Root Port profile。
- xHCI。
- HDA。
- MCE bank。
- CPU hotplug I/O。

`--dry-run` 实际上不会：

- 部署 `/opt`。
- 创建网络。
- define domain。
- 写 backup。

它只：

- 读取 libvirt 域。
- 读取构建产物。
- 在内存中变换 XML。
- 输出 diff。

这一点实现与 README 基本一致。

## 17.2 应用过程不是事务性的

非 dry-run 流程顺序是：

1. 部署 runtime。
2. 写 backup。
3. 在内存修改 XML。
4. 销毁/重建专属网络。
5. `virsh define`。
6. 删除旧网络。

如果在第 4 或第 5 步失败：

- `/opt` runtime 已部署。
- 新网络可能已创建或旧网络已被销毁。
- 域可能仍保持旧 XML。
- 没有自动恢复原网络。
- 没有自动回滚 runtime。
- backup 存在，但不会自动 `virsh define` 回去。

因此当前只有“备份”，没有真正的事务回滚。

建议后续引入：

- 预生成全部临时文件。
- `virsh define` 前先用 libvirt schema/define validation。
- 网络 XML 先比较，尽量不 destroy 正在使用的同名网络。
- 记录原 network XML/autostart/active state。
- define 失败时恢复网络。
- runtime 使用临时目录部署，成功后原子 rename。
- 最后再清理旧 profile。

## 17.3 XML 修改范围过大

`set_devices()` 会主动删除：

- serial
- parallel
- redirdev
- rng
- panic
- filesystem
- vsock
- shmem
- crypto
- iommu
- 某些 console
- virtio channel
- spice channel
- virtio-serial controller
- PS/2/tablet/input

这些并不都是“身份泄露”，有些可能是用户有意配置：

- `<rng>` 可能被 Guest 安全功能依赖。
- `<filesystem>` 可能是共享目录。
- `<vsock>` 可能有业务用途。
- `<iommu>` 可能是设备直通需求。
- `<panic>` 是故障处理。
- `<serial>` 可能用于调试。
- `<shmem>` 可能用于 Looking Glass。

当前脚本更像“将 VM 强制收敛到一个固定模板”，而不是“只修改 spoof 所需字段”。

后续应分为：

- 必须禁止的设备。
- 可选但有虚拟化特征的设备。
- 用户保留设备。
- 与 profile 冲突才拒绝的设备。

默认不应静默删除广泛的用户配置。

## 17.4 CPU pinning 过于简单

没有既有有效 pinning 时，当前会选择最低编号 CPU：

```python
selected = set(ordered[:count])
```

这没有考虑：

- P/E 核。
- SMT sibling。
- NUMA。
- LLC domain。
- isolated CPU。
- housekeeping CPU。
- emulator thread 与 vCPU cache locality。
- I/O thread。
- GPU/网卡所在 NUMA node。

在当前 i9-12900H 混合架构宿主上尤其需要改进。否则：

- Guest CPUID 是 host-passthrough。
- vCPU 可能落在不同 core type。
- 性能和 CPUID行为可能不稳定。
- timing 分布可能更明显。

## 17.5 NVRAM 生命周期问题

V2 会为 profile 生成新的 NVRAM 文件名，并设置 template，但如果目标文件已经存在，libvirt 不一定会根据新 template重新创建。

需要明确：

- profile 变化后旧 NVRAM 是否删除或归档。
- template 变化是否实际生效。
- 重复 apply 是否幂等。
- failed define 后是否留下未使用 VARS。
- Secure Boot 启用后 VARS 与 swtpm 如何一起迁移。

---

# 十八、README 与实际实现不一致之处 （这是有意设计）

## 18.1 V2 README

### “无 fallback”并不完全成立

当前 SATA 缺失时：

- Intel 固定派生 `51d3`
- AMD 固定派生 `7901`

位置：

- `01_generate_identity.py:1068-1087`

这虽然不是通用四选一 fallback，而是平台派生，但仍然不是宿主观察事实。

如果你的新规则是“没有事实就失败”，这部分必须重新定义：

- 要么明确它是 Q35 backend identity，而非宿主事实。
- 要么 catalog 根据选定 Guest platform 提供。
- 要么直接失败。

### “Type 8/9 原样继承”不代表正确

确实复制了 body/strings，但没有证明与 Guest PCI 拓扑一致。

### “内存拓扑已修复”不完整

如前所述：

- Type 16 maximum capacity 仍错误。
- Type 16 device count 仍错误。

### NVMe 支持表述需要实测

profile 有 NVMe catalog，但 QEMU NVMe identify path没有明确 patch。

## 18.2 Nika README

Nika 的主要问题：

- 声称 VMAware 100% undetected，但没有保存原始测试结果。
- 没有列出测试所用精确 QEMU commit、内核 commit、OVMF commit。
- 没有真机对照。
- 没有每项检测映射到具体补丁。
- 没有区分 QEMU spoof、KVM patch、GPU passthrough、EDID、网络的贡献。
- xHCI 日志声称写真实 Intel/AMD ID，实际代码写随机 ID。
- Secure Boot build/变量导入后又关闭 `SecureBootEnable`。
- README 称 custom kernel mandatory，但脚本名与现有 `kernelpatch619.sh` 也存在文档漂移。
- 很多 XML 要用户手工修改，不能证明最终状态和脚本状态一致。

因此 Nika 的 README 更适合作为“检测面和操作清单”，不能作为实现正确性的证据。

---

# 十九、Nika 中值得吸收的内容

下面这些方向值得进入 V2 后续规划。

## P0 值得研究

1. **KVM/CPU 检测矩阵**
   - CPUID latency。
   - MSR。
   - RDPMC。
   - APIC/IOAPIC。
   - VMCALL。
   - debug registers。
   - MONITOR/MWAIT。
   - XCR0。

2. **Secure Boot + TPM 完整生命周期**
   - 但不要复制宿主 PK/KEK/db。

3. **EDID 作为独立身份面**
   - 必须重算 checksum。
   - 必须按显示路径分类。

4. **SMBIOS 空槽位与 Type 16 语义**
   - Nika 至少意识到了空 DIMM 和物理槽位问题。

5. **完整外围检测面清单**
   - GPU UUID。
   - 路由器 MAC。
   - 默认网关 MAC。
   - 公网地址。
   - monitor/EDID。
   - USB controller/USB devices。

## P1 值得参考

1. 自定义设备模型而不是只改 ID。
2. SSDT 中补齐移动设备类别的思路。
3. 对 CPU hotplug port、MCE、ACPI debug、WAET 等非字符串面进行处理。
4. 不只处理 DMI，也检查设备行为和拓扑。

---

# 二十、Nika 中不应学习的内容

1. 不锁 commit 的分支构建。
2. 无匹配检查的批量 `sed`。
3. 全局替换 PCI vendor 宏。
4. 随机 PCI vendor/device ID。
5. 随机 ACPI OEM 字符串。
6. 固定 AMI/ALASKA 与任意主板组合。
7. 固定 DDR4、固定四槽。
8. 固定 cache。
9. 随机 sensor/fan/temperature。
10. 直接让 QEMU/SATA/HDA 冒充真实硬件但不实现行为。
11. 宿主 EFI PK/KEK/db/dbx 直接复制。
12. CPUID 全直透。
13. 无约束 MSR passthrough。
14. 不考虑 Guest vCPU 裁剪的宿主 APIC ID复制。
15. EDID 修改后不重算 checksum。
16. 直接覆盖全局 `/usr/local/bin/qemu-system-x86_64`。
17. 把大量不实例化设备的字符串替换算作有效 spoof。
18. README 声明替代实际 runtime 验证。

---

# 二十一、V2 当前确认的问题清单

## P0：应优先处理

### 1. SMBIOS Type 16 语义错误

- Maximum Capacity 写成 Guest 当前容量。
- Number Of Memory Devices 写成已安装 Guest DIMM 数。
- 已有宿主数据未使用。

### 2. 声卡 XML 策略与最新需求相反

- 当前强制删除 `<sound>`。
- 当前强制注入 ICH9 HDA。
- 当前 profile 验证要求 HDA。
- 应改成“只 patch 后端，不管理用户声卡存在性”。

### 3. 平台身份模型与“移动平台合理随机”冲突

- 当前强制继承 Lenovo DMI。
- 当前 `validate_inherited_platform` 会拒绝任何 catalog 平台。
- 需要重构平台来源模型，而不是加几个随机字段。

### 4. “无 fallback”目标尚未满足

- Intel/AMD 派生 AHCI ID 是事实缺失后的固定映射。
- `host_memory_kib()` 返回 0 也是潜在静默 fallback。
- `host_dmi()` 读取失败时先放空字符串，虽然下游部分会失败，但策略不统一。

### 5. Q35/现代 PCH 的行为边界没有测试体系

- 当前验证主要是 PCI ID/BAR/capability。
- 尚未验证 chipset register behavior。
- AMD 路径尤其缺少运行时证明。

### 6. XML 应用不是事务性的

- network 和 runtime 可能在 define 失败后残留。
- 没有自动回滚。

## P1：重要一致性问题

### 7. Type 8/9 与 Guest 拓扑未关联

### 8. Type 7 cache 与 hybrid CPU/Guest vCPU 不一致风险

### 9. Root Port 分配没有按下游端点类型建模

### 10. NVMe identity 生成与实际 QEMU NVMe identify path尚未闭环

### 11. CPU pinning 不理解 P/E core、NUMA、SMT、LLC

### 12. HDA 仍只有 ID/pin graph，没有 Realtek vendor behavior

### 13. xHCI 仍是通用 core + capability profile

### 14. OVMF 无 Secure Boot、无自动 TPM/swtpm

### 15. 电池/fan/thermal 只是静态快照

### 16. AMD 路径只做静态分析，没有生成产物和实机验证

## P2：工程和可维护性

### 17. XML 修改范围过大

会删除多个可能有用的用户设备。

### 18. 二进制仍包含大量 QEMU/KVM/VirtIO/构建路径字符串

这不一定是 Guest 可利用漏洞，但 README 应准确描述边界。

### 19. 配件池太小

当前大致是：

- DDR4/DDR5 各少量型号。
- SATA 4 个。
- NVMe 4 个。
- SCSI 2 个。
- CD-ROM 3 个。
- HID 3 套。
- 网关 OUI 少量。

不足以支持大规模、合理且不重复的整机组合。

### 20. 测试主要集中在设备 smoke/qtest

缺少：

- 完整 QEMU+OVMF boot。
- Windows Device Manager。
- WMI/PowerShell。
- `dmidecode`/ACPI dump。
- PCI config dump。
- driver initialization trace。
- reboot/cold boot。
- suspend/resume。
- FLR/reset。
- Secure Boot/TPM。
- clean baseline 重放。

---

# 二十二、建议的后续改进顺序

## 第一阶段：先修正事实模型

1. 修复 Type 16。
2. 建立 physical slot 与 installed Guest DIMM 分离模型。
3. 重新设计 platform profile：
   - host facts 作为约束。
   - catalog platform 作为 Guest 整机身份。
4. 删除不符合“无 fallback”的路径。
5. 将 HDA 从 XML 强制策略改为可选后端 patch。
6. 明确 Q35 backend identity 与 Guest platform identity 的边界。

## 第二阶段：建立平台 catalog

每个平台条目不能只是一个产品名，应至少包含：

- OEM。
- product/family/SKU/version。
- board vendor/name/version。
- chassis type/version。
- BIOS vendor/version/date/release。
- EC release。
- ACPI OEM ID/table ID/creator。
- CPU generation约束。
- DDR generation/form factor/speed范围。
- 最大容量和 slot count。
- Host Bridge/LPC/SMBus/SATA/xHCI/HDA。
- PCI BDF layout。
- subsystem vendor/device。
- Root Port 列表。
- battery/EC/thermal能力。
- 可搭配存储/HID/显示/网卡。
- firmware/logo。
- 移动/桌面类别。

随机应发生在“完整平台条目”和“该平台允许的配件集合”之间，不应逐字段随机。

## 第三阶段：建立运行时验证矩阵

至少采集 Guest 中：

- SMBIOS binary dump。
- ACPI tables。
- PCI config space。
- CPUID leaves。
- MSR 行为。
- APIC/IOAPIC。
- storage identify。
- HDA codec verbs。
- xHCI capabilities。
- NIC NVM/PHY/registers。
- UEFI variables。
- TPM state。
- EDID。
- Windows WMI/Device Manager/System Information。

然后与：

- profile。
- QEMU build-info。
- OVMF build-info。
- catalog。
- 预期 capability contract。

逐项对比。

## 第四阶段：扩展 Nika 有而 V2 没有的检测面

建议顺序：

1. Secure Boot + swtpm。
2. KVM检测测试工具，不先改内核。
3. IOAPIC/LAPIC/CPUID/MSR 最小补丁。
4. EDID 模块。
5. GPU passthrough identity验证。
6. 外部网络/网关身份验证。
7. 必要时再研究更深的 chipset behavior。

---

# 二十三、最终评价

## V2 已经做得更好的部分

- 单一身份来源。
- 唯一字段与非唯一字段区分。
- 完整 SMBIOS stream。
- 严格排他加载。
- QEMU/OVMF 配对。
- 源码锁定。
- 构建产物哈希。
- 独立 runtime。
- fail-fast。
- Root Port 实例化 profile。
- Intel/AMD xHCI capability 区分。
- 自定义 I226-V。
- 存储能力与身份同步。
- Host Bridge/LPC/CPU hotplug QEMU/OVMF 同步。
- dry-run。
- XML 基线漂移检测。

## V2 还没有从 Nika 学到的关键东西

不是那些大范围字符串替换，而是：

- 内核/KVM 检测面。
- Secure Boot/TPM 完整路径。
- EDID。
- 更广泛的外部身份面。
- 从“静态枚举正确”扩展到“指令、寄存器、timing、reset 行为正确”。
- 对整机身份从 DMI 到 PCI、ACPI、固件、外设的 catalog 化建模。

## 最重要的方向判断

V2 后续不应退回 Nika 的“随机替换一切”模式。

应继续保持：

```text
事实采集
→ 平台约束
→ 完整平台选择
→ 唯一身份生成
→ QEMU/OVMF/SMBIOS/XML统一编译
→ 运行时验证
```

并将 Nika 的额外覆盖面作为待验证模块逐个加入。

当前最先要改的不是 KVM 内核，而是：

1. Type 16 内存语义。
2. 声卡 XML 策略。
3. 平台 catalog 与移动平台随机模型。
4. 去除派生 fallback。
5. Q35/Intel/AMD 行为边界测试。
6. XML 部署事务化。
7. NVMe 与 Root Port 的实际闭环验证。

这些基础问题修正后，再进入 Secure Boot/TPM、KVM 内核和 EDID，整体架构才不会重新产生跨层矛盾。
