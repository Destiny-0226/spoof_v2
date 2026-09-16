# spoof_v2 与 Nika：共同核心模块复核与优化准备

日期：2026-09-16  
工作目录：`/home/lx/桌面/ovo_spoof/spoof_v2/`  
性质：只读源码分析、现有数据静态核对、后续改进建议；不是实施方案或运行效果证明。

## 1. 本轮范围与结论

按最新要求，本轮仅比较双方共有的 **QEMU、OVMF、XML，以及直接关联的身份生成、SMBIOS、ACPI、构建和部署链路**。

- **完全排除 Nika 的 KVM 内核补丁**：不审查其补丁内容、不评价效果、不列入能力差距、不据此安排当前优先级。
- **不比较 EDID 等外围身份方案**，不做显示器、GPU UUID、物理路由器身份等功能扩展建议。
- QEMU 内部的 Root Port、存储、xHCI、HDA、网卡模型，仍属于共同 QEMU 实现的一部分；仅讨论其工程一致性和验证边界，不展开外围设备专题。
- 不评审 Nika 的游戏功能、memflow、可执行程序；不执行双方构建/应用脚本，不启动或修改虚拟机，不操作系统服务。
- 不再采用 `进一步修改意见(废弃).txt` 作为当前需求来源。不覆盖旧对比文档，保留原批注。

**核心判断：spoof 应保留当前“宿主非唯一平台事实 + 新的唯一身份 + Guest 资源配置”的框架，继续完善跨层一致性、XML 修改边界和失败恢复；不应为了追赶 Nika 的功能数量，改回随机整机平台或大范围字符串替换。**

这个判断针对已核对的源码组织和校验机制，不代表已证明任何一方在所有 Guest、所有驱动或检测程序上的表现更好。

### 1.1 本次最重要的修正

1. 旧报告把“继承宿主平台”判为偏离需求，应撤回；这正是当前框架。
2. Guest DIMM 不照搬宿主容量和条数是有意设计；Type 16 不能仅因“不等于宿主”就定性为错误。
3. 现有拆分算法是**大规格优先**：16 GiB 当前得到 `1×16`，不是固定 `2×8`。批注中的“可以拆成 2×8”应保留为可选规则讨论，不擅自当作已确定需求。
4. Nika RTL8125 **存在迁移状态及版本定义**，旧报告“缺少 migration/version 管理证据”不准确。
5. spoof 并非毫无边界的严格验证系统：`replace_once` 只限制替换次数，不能证明原文只有一个匹配。
6. XML 方面，比“模板修改得多不多”更值得优先处理的是：**已有 vCPU 绑定关系可能重排、所有 `-acpitable` 参数被一并移除、网络重建与 domain define 没有组成可恢复事务。**

## 2. 证据方法与当前样本

下文使用四种标签：

- **源码确认**：能指向本次文件中的实际实现。
- **产物静态确认**：读取现有 JSON/二进制结构得到的事实，不代表 Guest 已实际使用。
- **设计取舍**：在当前需求下允许存在，不自动计为缺陷。
- **待验证风险**：源码提示可能存在问题，但未在本轮运行复现。

源码引用均相对本报告所在目录，格式为 `文件:起始行`。行号对应本次快照；后续修改源码后应结合函数名定位。

### 2.1 已静态确认的版本与数据

| 项目 | 本次核对结果 | 依据 |
|---|---|---|
| QEMU 构建目标 | `v11.0.2`，patch revision `32` | `02_patch_qemu.py:22` |
| OVMF 构建目标 | `edk2-stable202602`，patch revision `14` | `03_patch_ovmf.py:21` |
| 身份模型 | schema `23`，platform source version `3` | `01_generate_identity.py:36` |
| 平台来源 | `host-non-unique` | `artifacts/identity-hardware.json:1466` |
| 当前 Guest 内存拆分 | `[8192]` MiB，即一条 8 GiB | `artifacts/identity-hardware.json:1721` |
| profile 中宿主内存数组参考信息 | 2 槽、32 GiB 最大容量 | `artifacts/identity-hardware.json:1726` |
| 现有 SMBIOS 二进制 | Type 16 最大容量字段为 8 GiB，设备数 1；Type 17/19/20 各 1 条 | 本轮只读解析 `artifacts/smbios.bin` |
| profile 与构建记录关系 | QEMU、OVMF 的 `profile_sha256` 均与当前 JSON 实际哈希相同 | `build/qemu/build-info.json:3`、`build/ovmf/build-info.json:3` |

现有 SMBIOS 中另有 Type 7 三条、Type 8 十七条、Type 9 五条。这里只确认结构计数，不能由此证明 cache/插槽与 Guest 拓扑一致。

本轮**没有**重新构建、运行 qtest、启动 Guest、执行 `04 --dry-run`、读取实时 domain XML，也没有重新校验大型 QEMU/OVMF 二进制的全部哈希。构建记录与 profile 一致，不等于当前运行的虚拟机已经使用这些产物。

## 3. 如何处理旧报告和你的批注

| 旧结论或批注 | 本轮处理 | 理由 |
|---|---|---|
| 平台继承与“移动平台合理随机”冲突 | **撤回作为缺陷的判断** | `spoof_v2 vs Nika.md:240` 已明确旧需求过时；`01_generate_identity.py:2155` 正在主动保证平台继承关系 |
| Type 16 设备数必须等于宿主物理槽数 | **撤回绝对判断** | `spoof_v2 vs Nika.md:377` 明确要围绕 Guest 内存建模；Guest 的虚拟数组不必复制宿主数组 |
| Type 16 最大容量等于 Guest 当前容量必然错误 | **改为语义约定与边界校验问题** | 如果 Guest 数组被定义为固定、不可扩容的虚拟数组，这一值可以是设计结果；若以后支持扩容/热插拔，则必须重新定义上限 |
| “无 fallback”意味着所有缺失都必须报错 | **不再这样推导** | `spoof_v2 vs Nika.md:1497` 标注“有意设计”；应区分必需事实缺失、平台派生值和非实例化模型默认值 |
| 必须改成随机平台 catalog | **移出本轮优化方向** | 当前 catalog 应继续服务可更换配件，不替换宿主平台事实 |
| 声卡增删违反最新需求，必须撤销 | **不沿用旧需求作强制整改依据** | 当前实现明确选择板载 HDA 管理策略；本次用户未重新提出旧的声卡保留规则，不能擅自恢复 |
| Nika RTL8125 缺少迁移/版本定义 | **更正** | `Nika/Nika-Read-Only/rtl8125.c:1191` 有 VMState v2，`Nika/Nika-Read-Only/rtl8125.c:1280` 已挂接 |
| Secure Boot 关闭就是当前必须补齐的差距 | **改为固件策略差异** | spoof 明确要求关闭；Nika 编译相关能力后同样在 VARS 处理时关闭使能变量 |

第三处批注覆盖的是旧报告整个章节，具体认可到哪些子项并不完全明确。本报告不把它扩张成“所有风险都无效”，也不忽略它继续要求一律移除派生值：保留既定策略，对可验证的一致性问题单独列项。

## 4. 共同模块总览

| 共同部分 | spoof 当前实现 | Nika 当前实现 | 本轮判断 |
|---|---|---|---|
| 身份来源 | 统一 JSON、宿主平台事实、独立唯一身份 | 共享部分 `vars.sh` 变量，另有脚本随机值与手工 XML | spoof 更容易表达和检查一致性 |
| 源码版本 | 指定 tag、检查工作树与 HEAD、记录 revision | QEMU 使用 `stable-11.0` 分支；固件有上游与 Fedora 两条来源 | spoof 可追溯性更强，但不是已证明字节级可复现 |
| SMBIOS | 独立生成完整 stream，排他加载 | 修改 QEMU 内建生成器并配合 XML `-smbios type=` | 保留 spoof 的单一来源；加强跨表与 Guest 拓扑核对 |
| QEMU 平台 | Q35 实现上的 profile 身份与部分能力适配 | Q35 基础上固定/随机 ID 与较广范围替换 | 两者都不能仅凭外观字段声称完整实现现代芯片组 |
| PCIe 拓扑 | 每端口 profile、校验 Guest target；仍按顺序分配宿主端口 | 脚本替换加手工 XML 配置 | spoof 管理更系统，但端口对应关系还需语义校验 |
| QEMU 控制器 | profile 驱动的 xHCI/HDA/存储路径 | 分散的型号/ID/字符串修改 | 比较字段最终是否落到实际实例，而不是修改行数 |
| 网卡模型 | I226 设备、profile 校验、寄存器 qtest、VMState | RTL8125 自定义设备、寄存器/描述符处理、VMState v2 | 两者均有行为实现；不能把 Nika 简化成“只改 ID” |
| OVMF 配对 | 与同一 profile 绑定固件、Host Bridge/LPC 等字段 | `vars.sh` 共用部分值，固件还有固定品牌与独立处理 | spoof 的配对链路更明确 |
| XML | 自动校验、转换、部署、备份 | README 指导逐项手改，使用共享路径 | spoof 自动化更强，同时承担更大的状态修改责任 |
| 失败恢复 | 有备份、临时文件清理；未构成跨资源事务 | 手工流程、全局产物路径 | 两者均不能仅凭文件备份声称完整回滚 |

## 5. QEMU 与身份数据：保留架构，修正保证边界

### 5.1 单一 profile 是应该保留的主线

spoof 对 DMI、固件、主板等平台字段与宿主采集结果做显式一致性校验，而不是独立随机制造商、主板和 BIOS。代码还通过平台 ID 绑定这些字段。依据：`01_generate_identity.py:2155`。

Nika 的 `vars.sh` 确实有跨 QEMU/OVMF 共用变量的作用，不能说它“完全没有共享身份”。但它保存的是一组局部变量，不是涵盖全部身份、设备实例、资源布局和构建产物的统一模型。依据：`Nika/Nika-Read-Only/qemupatch.sh:32`、`Nika/Nika-Read-Only/ovmfpatch.sh:9`。

**建议方向：** 不增加随机平台 catalog；为现有字段明确标记“宿主观察、Guest 配置、平台派生、唯一身份生成、模型默认”五类来源。收益是解释性和校验清晰，而不是更多随机化。

### 5.2 版本控制强于 Nika，但“可追溯”不等于“字节级可复现”

spoof QEMU 检查 VERSION、tag 对应 HEAD 和 Git 工作树干净状态；应用阶段检查 profile/hash、平台、运行前缀、最低 patch revision 和产物哈希。依据：`02_patch_qemu.py:140`、`04_apply_spoof.py:556`。

Nika QEMU 克隆 `stable-11.0` 分支，随后复制工作树并修改；最终覆盖共享 QEMU 安装位置。依据：`Nika/Nika-Read-Only/qemupatch.sh:338`、`Nika/Nika-Read-Only/qemupatch.sh:515`、`Nika/Nika-Read-Only/qemupatch.sh:2311`。

需要收紧旧报告用词：

- spoof 当前验证 tag 与本地 HEAD 的关系、记录实际 revision，不等于已经建立独立可信的源码哈希白名单。
- 哈希绑定证明“这份构建记录对应这份文件”，不证明不同机器、时间、工具链再次构建必然得到相同二进制。
- patch revision 是最低门槛检查，不是所有将来 revision 之间都已证明兼容。

**后续验收：** 同一 profile 的重复构建需要记录工具链、依赖、环境差异；若要求字节级可复现，应做两次构建比较后再宣称。当前不需要因此重构四阶段流程。

### 5.3 新发现：`replace_once` 不是唯一匹配证明

**源码确认。** QEMU 与 OVMF 的 `replace_once` 都调用 `re.subn(..., count=1)`，再要求返回次数等于 1。原文有两个匹配时，替换一次后返回值仍为 1，不会暴露多余匹配。依据：`02_patch_qemu.py:201`、`03_patch_ovmf.py:251`。

相比之下，`replace_literal` 先统计原文出现次数，能够检查预期基数。依据：`02_patch_qemu.py:213`。

因此旧报告“严格的 replace_once / replace_literal”不能不加区分地成立。该发现是维护防护缺口，**不是已经证明当前生成源码错误**。

**后续验收：** 为补丁匹配分别准备 0、1、2 次出现的夹具；只有预期基数通过。跨文件配对补丁还应在修改完成后检查结果，不能只依赖替换函数没有报错。

### 5.4 SMBIOS 内存：尊重 Guest 模型，不照搬宿主

**设计取舍。** README 明确规定 Type 16/17/19/20 描述 Guest 实际容量和虚拟 DIMM，不复制宿主物理容量和条数。依据：`README.md:32`。

代码按对应内存 catalog 中的大容量规格优先拆分，单条最小 4 GiB；当前 catalog 包含 16、32 GiB 等规格。依据：`01_generate_identity.py:84`、`01_generate_identity.py:1828`。

从该算法直接推导出的例子（并非本轮启动 Guest 的测试结果）：

| Guest 容量 | 当前算法结果 | 与批注的关系 |
|---|---|---|
| 8 GiB | `1×8` | 一致 |
| 12 GiB | `8+4` | 避免 `3×4`，一致 |
| 16 GiB | `1×16` | 不排除未来选择 `2×8`，但当前没有该选择策略 |
| 24 GiB | `16+8` | 是大规格优先，不是固定按 8 GiB 分块 |
| 32 GiB | `1×32` | 同上 |

**Type 16 的合理解释：** DMTF SMBIOS 的 Maximum Capacity 表示数组支持上限，设备数量描述该数组可用设备位置/连接；二者不是“必须等于宿主数组”的要求。若把 Guest 定义为不可扩容、无空槽的固定数组，8 GiB/1 设备可以与当前模型一致。若要支持空槽、内存热插拔或更大的最大容量，才必须区分最大容量与当前安装量。规范参考见文末 [S1]。

当前 profile 保留宿主数组 32 GiB/2 槽的信息，而输出流生成 8 GiB/1 设备。**这说明存在两个层次的数据，不自动说明输出错误。** 应让字段命名和文档明确区分 host reference 与 guest effective array，避免后续代码误用。

现有校验已检查表数量、handle 引用以及设备数与 DIMM 数量的一致性；该函数尚未完整检查每张表的容量值、地址区间覆盖、重叠和上限关系。依据：`01_generate_identity.py:1990`、`01_generate_identity.py:2092`。

**建议：** 保留 Guest 内存建模，补充总容量与区间校验。只有你明确希望 `16 GiB → 2×8` 时，才单独调整拆分策略，而不是借“修 Type 16”改变当前设计。

### 5.5 完整 stream 解决来源冲突，不自动解决拓扑真实性

spoof 的 `full-file` 路径排斥其他 SMBIOS 来源，避免与额外 `-smbios` 混合。依据：`02_patch_qemu.py:2661`。Nika 的 README 让用户手工配置多条 `type=` 参数，并同时依赖修改后的生成器；其示例甚至组合了 Gigabyte manufacturer 与 HP product，说明示例不能直接当一致性模板。依据：`Nika/Nika-Read-Only/README.md:376`。

spoof 还需明确两个边界：

- Type 4 的多个 socket 引用同一组三张 Type 7；“三个 cache 表存在”不能证明多 socket、混合核心或裁剪 vCPU 后的 cache 拓扑正确。依据：`01_generate_identity.py:2039`、`01_generate_identity.py:2055`。
- Type 8/9 保留宿主 body/strings 并重建外部 handle；这证明复制一致，不证明表内端口、插槽和 BDF 与 Guest 设备布局相符。依据：`01_generate_identity.py:2064`。

这里建议的是**跨层验证**，不是立即停止宿主继承或随机重写这些表。

### 5.6 Root Port：从稳定分配走向有意义的对应关系

**源码确认。** `guest_root_port_profiles` 对 Guest target 排序，再与 `host_ports` 进行 `zip`，记录 `guest_target_port`。依据：`01_generate_identity.py:920`。

其价值是每个端口有稳定、可校验的 profile，避免统一写一个 ID；但对应规则本身未按下游设备类型、端点链路要求建立。

**待验证风险：** 根端口最大能力、端点能力、Guest bus 和实际协商值是否一致，需要作为同一个关系检查，不能逐字段“等于宿主”就结束。不要把这一风险写成“当前某个 GPU/NVMe 已确定挂错”，本轮没有做实时枚举。

Nika 的 QEMU 两个脚本变体也不能当作完全相同：`qemupatch_homo.sh` 在 QEMU topology/ACPI APIC 映射部分放开了主脚本的 CPU vendor 条件。依据：`Nika/Nika-Read-Only/qemupatch.sh:2126`、`Nika/Nika-Read-Only/qemupatch_homo.sh:2126`。这里仅标记共同 QEMU 层的变体差异，不分析内核补丁，也不建议直接移植这段处理。

### 5.7 共同设备模型：评估实际落地，不比覆盖名单长度

| 路径 | 本轮源码事实 | 后续应验证什么 |
|---|---|---|
| xHCI | spoof 区分 Intel 集成形态/MSI 与 AMD PCIe 形态/MSI-X，并校验端口等 profile；Nika 日志显示固定 ID，实际替换却使用随机 `xhci` 变量 | profile → 修改后源码 → 实际实例的配置空间是否一致；不据名称宣称完整硬件复刻 |
| HDA | spoof 按 profile 放置 controller/codec，XML 层转换 `<sound>`，保留并关联 `<audio>` 后端 | 实例地址与 controller/codec/backend 是否一致；不因保留后端就声称保留原声卡配置 |
| SATA/SCSI | spoof 给实际模型写入型号/固件，并对 SCSI 设置可表达的 XML 字段；部分值仍是模型级而非任意逐盘属性 | 多盘是否可独立表达，不能把单盘成功推论到所有总线组合 |
| NVMe | spoof 配件池/配置记录不等于 NVMe Identify 已闭环；在本次主补丁脚本中未发现专门的 NVMe identity 修改，Nika 可确认的是控制器描述字符串替换 | 先核对实际 libvirt/QEMU 命令行及 Identify 内容，再决定是否存在能力缺口 |
| 自定义网卡 | spoof I226 有寄存器 qtest 和 VMState；Nika RTL8125 有完整设备文件及 VMState v2 | 在同等驱动、负载、复位和迁移条件下比较；目前不能给出完整硬件保真度排名 |

依据：`02_patch_qemu.py:1927`、`Nika/Nika-Read-Only/qemupatch.sh:1901`、`Nika/Nika-Read-Only/qemupatch.sh:1909`、`04_apply_spoof.py:1003`、`04_apply_spoof.py:1353`、`02_patch_qemu.py:1301`、`04_apply_spoof.py:1234`、`Nika/Nika-Read-Only/qemupatch.sh:1129`、`02_patch_qemu.py:221`、`02_patch_qemu.py:1241`、`Nika/Nika-Read-Only/rtl8125.c:1191`。

尤其需要更正：有 VMState 不等于完整迁移语义已验证；有 qtest 函数也不等于本轮已运行通过。spoof 的优势是**可见的校验入口更多、与 profile 绑定更清楚**，不是已经证明所有行为都强于 RTL8125。

### 5.8 派生值不是自动违规，但应显式限定适用平台

SATA 缺失时，spoof 按 LPC vendor 派生 Intel `51d3` 或 AMD `7901`，并标注 `derived-modern-ahci-for-q35`。依据：`01_generate_identity.py:1068`。

这与“缺失后随便换另一个品牌平台”不同，应尊重其有意设计。但当前分支主要以 vendor 区分，不能据此推断适合所有同厂商年代的平台。

建议保留该机制，补充可支持平台的边界说明与一致性检查；不要求一概删除 fallback，也不默认把这些值重新描述为宿主实测。

Q35/ICH9 后端边界已经写入 README。依据：`README.md:208`。因此，ID/BDF/AML 匹配与完整芯片组行为是不同层次，应在报告和未来测试中保持区分。

## 6. OVMF：配对构建是优势，固件状态要单独管理

### 6.1 应继续保留的配对关系

spoof 用同一 profile 驱动固件和 ACPI 字段，并配对 Host Bridge DID、LPC 地址、CPU hotplug I/O 等 QEMU/OVMF 共享约定；应用阶段再校验 profile 与构建记录关系。依据：`03_patch_ovmf.py:259`、`03_patch_ovmf.py:344`、`03_patch_ovmf.py:361`、`04_apply_spoof.py:556`。

这比只共享少量脚本变量更便于审计。后续优化应首先避免一侧补丁变了、另一侧仍使用旧产物，而不是扩大与实际 QEMU 启动路径无关的固件字符串替换。

### 6.2 不把 Nika 两条固件路线混成一个基线

- `ovmfpatch.sh`：来自上游 `edk2-stable202602` 分支。依据：`Nika/Nika-Read-Only/ovmfpatch.sh:55`。
- `fedk2patch.sh`：经 `fedpkg` 获取 Fedora `f44` 打包源并执行准备流程。依据：`Nika/Nika-Read-Only/fedk2patch.sh:53`。

二者来源和上下文不同。本轮不运行它们，也不把其中一个脚本的构建选项直接套给另一个产物。Nika 的优势是提供了实际部署入口与替代来源；代价是需要更明确的版本、产物来源与手工状态记录。

### 6.3 Secure Boot：是当前政策差异，不是默认整改项

spoof 明确拒绝 `secure_boot` 不为 false 的 profile，构建命令包含 TPM 支持但没有 Secure Boot 编译开关；XML loader 设置 `secure="no"`。依据：`03_patch_ovmf.py:430`、`03_patch_ovmf.py:453`、`04_apply_spoof.py:883`。

Nika 上游固件脚本编译 Secure Boot 能力，但随后处理 VARS 时执行 `--set-false SecureBootEnable`。依据：`Nika/Nika-Read-Only/ovmfpatch.sh:425`、`Nika/Nika-Read-Only/ovmfpatch.sh:537`。

因此不能从“有编译开关”直接写成“Nika 最终已启用”。本轮不建议为追平 Nika 而开启 Secure Boot、增加 TPM 生命周期或复制宿主信任变量；这些都需要独立需求。

### 6.4 模板、运行时 NVRAM 与重建不是同一件事

spoof 使用含 `nvram_id` 的文件名，设置 profile 对应模板；这提供隔离基础，但 `set_os` 本身只生成 XML，不实现现有运行时 VARS 的刷新/迁移决策。依据：`04_apply_spoof.py:888`。

**待验证风险：** 同一 profile 下固件模板变更时，是继续使用旧运行时 NVRAM，还是应创建新状态？需要明确规则并验证 libvirt 实际行为，不能看到 template 路径变化就认为 Guest 状态已经更新。

建议将“固件 CODE”“VARS 模板”“运行时 NVRAM”三者分别记录；默认不要为了追求一致性直接清空用户启动项等状态。

### 6.5 Nika 的失败信号值得反向借鉴

`ovmfpatch.sh` 缺少 `vars.sh` 时提示 aborting，却 `exit 0`。依据：`Nika/Nika-Read-Only/ovmfpatch.sh:9`。若被自动化封装，上层可能把“没有构建”视作成功；这是源码可确认的成功/失败信号问题。

spoof 应继续保持异常失败的明确性，同时为所有构建阶段提供“开始、校验、完成、产物清单”的可核对记录，避免把旧产物存在当成本次成功。

## 7. XML 与应用流程：当前最值得优先加固的部分

### 7.1 自动转换与手工指南，各有不同风险

Nika README 要求手工编辑 QEMU 参数、SMBIOS、CPU、emulator 和 ACPI 表等；共享二进制路径与手工 XML 的配套关系依赖操作者。依据：`Nika/Nika-Read-Only/README.md:345`、`Nika/Nika-Read-Only/README.md:498`。

spoof 将多数关系纳入校验与自动转换，能明显减少遗漏；但它不是只改几个标识字段，而是把 domain 收敛到一套管理策略。自动化越强，越需要清晰说明修改所有权与失败后的状态。

### 7.2 高优先级：已有 vCPU 映射会丢失顺序

**源码确认，未执行实际 apply。**

1. `configured_cpu_bindings` 读取原有 `vcpu -> CPU` 映射，但成功后只保留 `set(pins.values())`。
2. `set_vcpu_pinning` 删除原有 pin，再对 CPU 集合排序并从 vCPU 0 重新编号。

依据：`04_apply_spoof.py:171`、`04_apply_spoof.py:209`。

例如原来 `vCPU0 -> CPU3`、`vCPU1 -> CPU1`，这一路径可能重写为 `vCPU0 -> CPU1`、`vCPU1 -> CPU3`。CPU 集合没变，但绑定关系变了；不能称作完整保留用户 pinning。

这一点与 Nika 的共同 XML 场景直接相关：其 README 就给出非顺序 vCPU 绑定示例。依据：`Nika/Nika-Read-Only/README.md:549`。这里不评价该示例对某台机器是否正确，只用它证明非顺序映射是实际存在的输入形态。

**后续验收：** 合法旧映射在重复 apply 后逐项不变；缺失映射时才走自动策略；自动策略不能只用最低 CPU 编号代表拓扑合理性。

### 7.3 高优先级：`-acpitable` 清理没有区分所有者

**源码确认。** `set_qemu_commandline` 将任何 `-acpitable` 都标为待移除，并跳过后续参数，然后按自身 policy 加回生成表。依据：`04_apply_spoof.py:1326`。

移除旧 `-smbios` 有完整 stream 排他来源的明确理由；同样的全量删除规则用于 `-acpitable`，却可能清掉用户自定义、与当前生成表无关的 ACPI 表。

不能只因为变量名叫 `generated_acpi` 就认为它已经识别“本工具生成”。

**后续验收：** 明确选择并记录策略：只替换工具拥有的表，或在发现外部表时拒绝并说明冲突；若必须全量接管，也应事前列明被删除的参数，不静默处理。不能盲目保留有潜在重复定义的表。

### 7.4 高优先级：有 XML 备份，不等于跨资源事务

**源码确认。** 非 dry-run 顺序为：部署 runtime、写 XML 备份、转换 XML、重建网络、define domain、清理旧网络。异常路径会清理临时 `.new.xml`，但未组成恢复 runtime/网络/域状态的事务。依据：`04_apply_spoof.py:1443`。

`ensure_managed_networks` 对已存在的同名网络先 destroy/undefine，再 define/start，即使配置未变化也走重建流程。依据：`04_apply_spoof.py:689`。

**可能的后果，不是本轮已发生的故障：** 网络步骤成功、domain define 失败时，域与网络可能处于不同阶段；再次执行需要操作者判断恢复路径。重复应用也可能造成不必要的网络停启。

**后续方向：** 先形成完整变更计划，比较网络配置并识别无变化；保存原网络 XML、active/autostart 状态及域 XML；逐步记录已执行动作；失败时提供明确恢复结果。旧资源清理应在新配置确认后进行。

### 7.5 模板化删除应成为显式策略，不必一概禁止

`set_devices` 会删除 serial、rng、filesystem、vsock、shmem、iommu 等类型及部分其他设备。依据：`04_apply_spoof.py:1147`。

这是否错误取决于当前管理契约：若工具明确接管整套模板，它可以是设计；若用户认为只是“更新身份”，则会造成行为偏差。**本轮不复活旧需求，也不建议无条件保留一切设备。**

建议输出结构化变更说明，将动作分成：必须替换、策略移除、用户保留、冲突拒绝，并写明依据。已有 unified diff 是基础，但它不能替代“为什么删除”的解释。

### 7.6 声卡：准确区分 `<sound>` 与 `<audio>`

当前代码检查声卡类型与地址冲突，删除 libvirt `<sound>`，通过 QEMU command line 放置 controller/codec；随后还比较 `<audio>` 前后是否相同。依据：`04_apply_spoof.py:1003`、`04_apply_spoof.py:1353`、`04_apply_spoof.py:1380`。

所以两种说法都不准确：“用户声卡配置完全保留”“声卡后端也被全部删除”。正确表述是：**接管虚拟声卡实例，尽量保留音频后端配置。**

将它作为当前受控策略写清即可；是否增加用户自选/不接管模式，留待需求确认，不在本轮强行列为必须改架构。

### 7.7 dry-run 的价值与边界

当前 dry-run 分支在 runtime 部署和备份写入之前完成内存中的 XML 变换并输出 diff。依据：`04_apply_spoof.py:1434`。这是比照 README 手工修改更可靠的预览入口。

但它不启动 Guest，也没有在该分支完成所有实际资源创建或定义。因此“dry-run 成功”只能说明它执行的本地逻辑与检查通过，不能替代 libvirt 实际接纳、QEMU 启动或 Guest 枚举验证。本轮未运行它。

## 8. 面向后续优化的优先级

这里的 P0 表示**下一轮优化最先讨论和测试的工作**，不表示本轮已经观察到生产事故。所有项目都限定在共同核心，不附带外围或内核任务。

| 级别 | 项目 | 依据/性质 | 建议验收标准 |
|---|---|---|---|
| P0 | 保留完整 vCPU pinning 映射 | 已确认映射降为集合后重建 | 非顺序有效映射重复 apply 不变化 |
| P0 | 明确 ACPI 参数所有权 | 已确认所有 `-acpitable` 被移除 | 自定义表被保留、明确拒绝或明确列入接管计划，不被误判为自生成表 |
| P0 | 应用失败恢复与网络幂等 | 已确认 destroy/redefine 与 domain define 分离 | 网络无变化不重建；各失败点能恢复或准确报告残留状态 |
| P1 | 补丁匹配基数验证 | 已确认 `count=1` 的保证边界 | 0/1/2 次匹配夹具区分正确 |
| P1 | Guest 内存语义和容量区间校验 | 当前设计合理但保证不完整 | XML/profile/Type16/17/19/20 容量及引用一致；最大容量政策明确 |
| P1 | Root Port、Type9、Guest bus 联合校验 | 顺序映射不等于语义匹配 | 每端口有可解释的下游关系；无法表达的布局明确拒绝 |
| P1 | 存储身份按实际总线闭环 | 部分路径已实现，NVMe 仍需证据 | 核对最终命令行与 Guest 查询，不以 catalog 存在宣称完成 |
| P1 | 固件模板与运行时 NVRAM 生命周期 | 策略尚需明确/实测 | 重建、重复 apply、切 profile 的状态保留与更新行为可解释 |
| P2 | 字段来源与 XML 变更说明 | 可维护性改进 | 每个关键字段可解释来源；每类删除可说明理由 |
| P2 | 构建/测试证据归档 | 不等同于再造框架 | profile、源码、工具链、测试与产物记录可关联 |

**不纳入当前整改：** 改成随机整机 catalog、强制复制宿主 DIMM 数、为对齐 Nika 而开启 Secure Boot、无依据更换 xHCI/网卡后端，以及因旧批注而自动撤销现行 HDA 策略。

## 9. 建议验证矩阵：本轮均未执行

| 层次 | 后续验证案例 | 要证明的性质 |
|---|---|---|
| 补丁工具 | 匹配 0/1/2 次、重复执行、版本不支持 | 失败语义与适用版本明确 |
| SMBIOS | 8/12/16/24/32 GiB、无效容量、多个 socket | 保持已选内存政策；跨表容量、引用和拓扑一致 |
| XML 纯转换 | 非顺序 pinning、自定义 ACPI、多盘、已有音频后端 | 修改边界明确、有效用户状态不被误改 |
| XML 重复转换 | 同输入/同 profile 连续应用 | 无参数重复、无无意义差异 |
| 部署故障注入 | runtime、网络定义、网络启动、domain define、旧资源清理失败 | 失败后状态可恢复、残留可解释 |
| QEMU/OVMF 联合启动 | 同 profile 与不匹配 profile、不同已声明支持的平台 | 配对前置检查有效；正确组合可启动 |
| Guest 实际观测 | SMBIOS、PCI/ACPI 枚举、存储 Identify、设备驱动初始化 | 构建意图确实反映在实际运行实例中 |
| 固件状态 | 同 profile 重建、切换 profile、已有 NVRAM | 不把模板更新误当作运行时状态刷新 |
| 自定义设备 | 寄存器 qtest、复位、驱动负载、声明支持的保存/恢复 | 能力存在与行为正确分开证明 |

后续记录应至少包含样本 profile/hash、QEMU/OVMF revision、驱动和 Guest 版本、测试输入、结果与日志位置。不要用“看起来正常”替代可回溯的结果，也不要求在本轮启动这些测试。

## 10. 最终判断

在收敛后的共同范围内，spoof 最值得保留的是 **单一 profile、宿主平台事实继承、完整 SMBIOS 来源控制、QEMU/OVMF 配对和自动化前置校验**。

Nika 最值得借鉴的是：共同层面上实际配置路径的覆盖，以及设备模型进入寄存器/描述符行为层的意识；同时应反向学习其分散身份、手工配套和失败信号的问题，而不是整体照搬补丁。

下一步应先把“已选择的设计”与“实现未兑现的保证”分开。**优先稳固 XML 状态修改、补丁校验、Guest 数据闭环；不推翻当前架构，也不把独有外围功能和排除项重新塞进当前路线图。**

## 附录 A：核心输入指纹

用于识别本报告对应版本；不是对运行环境或构建二进制的完整验真证明。

| 文件 | SHA-256 |
|---|---|
| `spoof_v2 vs Nika.md` | `9f7ac45ce9250cfa74b3d0098604811db6e6746f5f0b0674214080b6d7062124` |
| `01_generate_identity.py` | `88b1b16188e5a5254dd33b7d70678c00ccbaa6fb1c5c0d2a1b51d1cc806a5948` |
| `02_patch_qemu.py` | `587e4d737ecede25b2ac6208816bfe1c89d55e15ab3ea08804ed0166a5cb704c` |
| `03_patch_ovmf.py` | `e9c78c2b2890ec4c8f05c79849ab5212b74e28d16e724cf283c46ee111973970` |
| `04_apply_spoof.py` | `2d58273bd645582c2a151f3df15aaab408bd6a849e56f05a8c974bb2d9b59eb4` |
| `artifacts/identity-hardware.json` | `971cfbc82404d9e3d1a37081795ea0fb92267532bd7c587f60a64700dfde8aea` |
| `artifacts/smbios.bin` | `eac1e07a63a63654e2f812495b7b349db90ece17719ad5c02db5b854e79367cb` |
| `Nika/Nika-Read-Only/qemupatch.sh` | `f5ec8269a1dc663876a84e662f8b3b1bca1804f37d3f3af6ba037fe8b3708728` |
| `Nika/Nika-Read-Only/qemupatch_homo.sh` | `944b91422fdf0f7ec74e3f294820c5d213cbff48c931dcdc88f7a0cbcc016f2f` |
| `Nika/Nika-Read-Only/ovmfpatch.sh` | `e969b46324da7d716f3c15b51b6541e23a0fc3a38efd1273482c17864282534b` |
| `Nika/Nika-Read-Only/fedk2patch.sh` | `bc207ce0fb9cc936d9d829acf0abc95f524469d666001b86027fcb389c4d5041` |
| `Nika/Nika-Read-Only/rtl8125.c` | `45d7244c745559dab4bafb450738e264e1c9a9c5401fd4506b6a2c9ba5a1c001` |

## 附录 B：规范参考

[S1] DMTF，*System Management BIOS (SMBIOS) Reference Specification*，DSP0134，版本 3.9.0，Physical Memory Array (Type 16) 章节。本报告仅用其澄清字段语义，不以“与宿主不同”替代 Guest 模型判断。

官方文档定位：`https://www.dmtf.org/sites/default/files/standards/documents/DSP0134_3.9.0.pdf`。

---

交付边界：本报告为新增文件；旧报告、四阶段脚本和 Nika 原始文件保持不变。所有后续优化与验证项目均为建议，尚未实施。
