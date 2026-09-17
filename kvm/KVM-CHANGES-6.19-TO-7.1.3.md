# Linux 6.19 -> CachyOS 7.1.3 KVM 更新分析

## 范围和方法

本文不对两棵完整内核源码做全量 diff。分析以 kernel.org 的
`ChangeLog-7.0`/`ChangeLog-7.1`/`ChangeLog-7.1.1..3` 为主，然后回到 KVM
维护者的合并提交和目标源码中的关键 API 进行定点核对。

这里主要描述上游 Linux KVM 的变化，同时检查 CachyOS 7.1.3-1
发布页列出的下游 topic branch，而不扫描整棵源码。补丁移植和编译验证
使用的是精确的 `linux-cachyos 7.1.3-1` 源码包。

> 7.0 开发周期最初以 6.20 命名，所以对应的 KVM 分支标签仍包含
> `6.20`；这不是分析了错误版本。

## 结论

6.19 到 7.1.3 不只是类型调整，KVM/x86 有三类对当前 Intel OVO
补丁有实际影响的变化：

1. **7.0 引入上游 mediated PMU**：PMU 能力结构和初始化 API 变了，上游开始
   提供受控的硬件 PMU 所有权切换。
2. **7.1 把 VMXON/VMXOFF 从 KVM 下沉到 x86 core**：加入引用计数，使
   KVM 与 TDX/其他内核虚拟化用户可共享 VMX 基础设施。
3. **MMU、MMIO、nested VMX 和 Hyper-V 的正确性/安全加固**：7.1.3 稳定版
   又带入了几个防止宿主崩溃、越界和 UAF 的 KVM 修复。

当前 `7.1.3-1.intel.patch` 的绝大多数改动可原样移植，少数位置因上述结构
变化做了定向改写，并额外适配了 7.0 新增的 RDPMC 动态拦截重算。这说明
大部分 VMX 路径稳定，但不代表 6.19 补丁可以不经语义审核地直接套用。

## Linux 7.0 周期

KVM 主合并提交汇入了 267 个提交（所有架构），其后还有修复批次。

### 1. Mediated PMU

上游新增 mediated PMU 模式：客户运行时可在 KVM 约束下获得 PMU
数据 MSR/计数器的直接访问，进出客户时由 KVM 切换 PMU 上下文。控制寄存器
仍保持拦截，以实施事件过滤和保护宿主。该模式默认关闭，且以 KVM
模块为粒度整体启用。

对目标源码的直接影响：

- `struct x86_pmu_capability` 增加 `mediated`。
- `kvm_init_pmu_capability()` 改为接收非 `const struct kvm_pmu_ops *`，
  因为初始化会根据能力调整 PMU ops。
- 新增 `enable_mediated_pmu` 模块参数和对应的 fast path。

OVO 的混合 P-core PMU 不是这个上游功能。当前移植明确设置
`cap->mediated = 0`，因此不会启动上游 mediated PMU 的 world switch。
OVO 的 PMU MSR 放行仍然有效。7.0 新增的动态 RDPMC intercept 重算曾会
覆盖旧补丁的初始 VMCS 设置；当前移植已补充对应处理，详见下文。未来可以
评估改用上游 mediated PMU，但这是行为重构，不是等价的类型替换。

#### 1.1 异构 CPU 开关的精确状态

- `kvm.enable_pmu=Y` 仍是全局 vPMU 总开关。
- `kvm_intel.enable_mediated_pmu=N` 是 7.0 新增的实现模式开关，不是
  “允许异构 CPU PMU”的开关，而且是只读模块参数。
- 原版 7.1.3 的 `kvm_init_pmu_capability()` 看到
  `X86_FEATURE_HYBRID_CPU` 后仍会把 `enable_pmu` 置为 `false` 并清空
  `kvm_host_pmu`。`perf_get_x86_pmu_capability()` 本身也明确拒绝枚举 hybrid
  PMU，所以 KVM 没有自动选择 P-core/E-core PMU 的能力。
- `KVM_CAP_PMU_CAPABILITY` 当前只允许用户空间按 VM 设置
  `KVM_PMU_CAP_DISABLE`，没有选择 core type 或允许 hybrid PMU 的 ABI。
- CachyOS 7.1.3-1 配置了 `CONFIG_PERF_GUEST_MEDIATED_PMU=y`，说明 mediated
  框架已编入，但不改变上面的 hybrid 拒绝逻辑。

因此 OVO 的 `allow_hybrid_pmu` 在 7.1.3 上仍有实际作用；它不是被上游新
开关取代了。当前实现通过遍历在线 CPU、只接受一致的 P-core CPUID.0A
能力来合成 `kvm_host_pmu`，但执行阶段仍依赖用户空间把所有 vCPU 严格绑定
到这些 P-core，KVM 内核尚未强制执行这个约束。

#### 1.2 RDPMC 的 7.0 移植语义冲突及修复

7.0 的 mediated PMU 系列新增了 `vmx_recalc_instruction_intercepts()`。
用户空间设置 vCPU CPUID 后，KVM 会发出 `KVM_REQ_RECALC_INTERCEPTS`；随后
该函数按 `kvm_need_rdpmc_intercept(vcpu)` 动态更新
`CPU_BASED_RDPMC_EXITING`。只要 vCPU 没有 mediated PMU，这个判断就返回
需要拦截。

当前 OVO synthetic capability 设置 `mediated=0`。如果只保留旧补丁在
`vmx_exec_control()` 初始化阶段清除 `CPU_BASED_RDPMC_EXITING` 的修改，后续
动态重算就会把它重新置位，使 RDPMC VM-exit 后由 `kvm_emulate_rdpmc()`
处理，而不是 6.19 补丁预期的硬件直接执行。

这不等同于所有 PMU passthrough 都失效：当前
`vmx_recalc_pmu_msr_intercepts()` 在 `enable_mediated_pmu=false` 时直接返回，
所以 OVO 对 PMC/PERFEVTSEL MSR bitmap 的修改不会被它覆盖。当前补丁已将
`vmx_recalc_instruction_intercepts()` 改为无条件调用
`exec_controls_clearbit(..., CPU_BASED_RDPMC_EXITING)`。这样初始 control 和每次
动态重算都会关闭 RDPMC exiting，与 6.19 OVO 补丁的无条件直通行为一致。

### 2. APIC/IOAPIC 整理

- 移除 `MAX_NR_RESERVED_IOAPIC_PINS`，相关代码改用 `KVM_MAX_IRQ_ROUTES`。
- 修正 SMM 状态下 memslot 选择、dirty marking 和若干可由客户触发的
  旧 `ASSERT`/`WARN` 路径。
- 调整 nested VMX 中 L2 运行期间的 VMCS/APICv 更新方式。

这导致 OVO IOAPIC 修改的一个上下文冲突；移植保留了 7.1.3 的
`KVM_MAX_IRQ_ROUTES` 表达，只应用 OVO 需要的 pin/version 行为。

### 3. VMX、x86 和 guest_memfd

- VMX 向客户正确反射 SGX EPCM page fault，修复 posted-interrupt 唤醒注册失败，
  并加强 nested VMCS 字段验证。
- x86 KVM 加强 L2 运行时 CPU model 切换、PV MSR、CR3/async-PF、PDPTR
  和 CPU capability 处理。
- 暴露更多新 Intel CPUID 能力，包括 AVX10/AVX10.2、AMX 新叶和 MOVRS。
- guest_memfd 简化了 preparation/大页跟踪，并统一 SNP/TDX 内存初始化
  的页对齐和 GUP 处理。

OVO 的 raw CPUID 路径会绕过 KVM 的部分 CPUID policy，因此“上游已支持
新 CPUID bit”不等于 OVO 路径自动获得了相同的一致性保证。混合 CPU 上仍应
保持 vCPU P-core 绑定并验证各 vCPU 观察到的 CPUID 一致性。

## Linux 7.1 周期

KVM 主合并提交汇入了 373 个提交（所有架构），之后又有修复批次。

### 1. VMXON/VMXOFF 下沉到 x86 core

上游将 VMXON/VMXOFF 和 EFER.SVME 切换从 KVM 移到 `arch/x86/virt`，
同时加入简单引用计数。目标是让 TDX/trusted I/O 等非 KVM 的内核用户在
未加载 KVM 时也能使用 VMX，且不会与 KVM 重复开关硬件虚拟化。

这是本次移植最重要的语义冲突：6.19 补丁在 `kvm_cpu_vmxon()` /
`kvm_cpu_vmxoff()` 附近管理 dummy DS area，这两个 KVM 局部生命周期点在
7.1 已不存在。当前移植改为挂在 KVM 的
`x86_virt_get_ref(X86_FEATURE_VMX)` / `x86_virt_put_ref(...)` 包装层，不修改
共享的 `arch/x86/virt/hw.c`，避免影响 TDX 等其他 VMX 用户。

### 2. MMIO 持久化修复

KVM 将单块 MMIO write 的值复制到持久的 fragment 字段，修复退出到用户空间
后可能使用失效栈内存的问题。因此 7.1.3 的 `struct kvm_mmio_fragment`
含有 `u64 val` 和 `unsigned int len`。OVO 仅在相邻位置添加缓存结构，
移植保留了上游 fragment 布局，没有把 6.19 的旧结构覆盖回去。

### 3. MMU、VMX 和 x86 正确性

- MMU 修正模块参数 UB 警告、拆分大页页表的分配和 2 MiB 映射时的
  无意义同步。
- VMX 移除过时的 branch-hint prefix，并修正 Clang 下 VMCS write 的汇编
  operand 约束。
- 新增 AVX512 BMM 暴露，修正 SMM 中 CPUID faulting 规则，并在已有异常
  等待注入时拒绝 `SET_GUEST_DEBUG`。
- 后续修复包括 shadow paging UAF、LAPIC 保护、vCPU load 时的全局时钟
  更新限速和 APIC bus frequency 报告。

OVO 的 VM-exit 汇编快路径仍能在 7.1.3 的布局上编译，但已有的 objtool
警告仍然存在；这是需要运行时压测覆盖的快路径，不应仅以“编译成功”
判定安全。

## 7.1.1 -> 7.1.3 稳定版增量

`ChangeLog-7.1.1` 和 `ChangeLog-7.1.2` 中没有以 KVM 开头的提交标题。
`7.1.3` 有 6 个 KVM 标题提交，其中 5 个与 x86/通用 KVM 有关：

| 稳定版提交 | 作用 | 与 Intel OVO 的关系 |
|---|---|---|
| `2753a097d1fe` | 限制 SEV debug encryption 的页内长度，防止中间缓冲区溢出 | AMD/SEV，对 Intel 无直接影响 |
| `f636cf6a1e7b` | 检查 Hyper-V sparse bank 索引上界，防止越界读和错误 TLB flush | 使用 nested Hyper-V enlightenment 时有效 |
| `5c87b4737468` | 用 `get_unaligned()` 替代 ioeventfd datamatch 中客户可触发的 `BUG_ON()` | 保留在当前移植中，防止宿主崩溃 |
| `b2ae3245ea44` | 在计算大页最大映射级别前，确认 base GFN 属于 memslot | 保留，防止 `lpage_info` 越界/宿主 page fault |
| `1ae7d5a6db6c` | 修正 shadow page 以意外 role 复用后的残留 rmap UAF | 保留，对 shadow paging 工作负载有效 |
| `8ead17358119` | arm64 zero-page stage-2 标签同步优化 | 与 x86 Intel 无关 |

这些修复所在的实现未被 OVO 补丁替换，因此当前 7.1.3 移植能继承它们。

## CachyOS 7.1.3-1 下游 KVM 增量

CachyOS 的官方发布记录显示该包基于 Linux 7.1.3，并列出了所有
topic branch 提交。按 KVM 路径/标题筛选后，只有一个额外的明确
KVM 改动：

- `e9fdac7d0820 x86/kvm: Disable preemption in kvm_flush_tlb_multi()`。
  它属于 CachyOS `7.1/preempt-ipi` 系列，在 `arch/x86/kernel/kvm.c`
  的 KVM **guest** paravirt TLB flush 函数中使用 `guard(preempt)()`，保护
  per-CPU `__pv_cpu_mask` 和当前 CPU 上下文。它与系列中“通用
  `smp_call_function*()` 更早恢复抢占”的改动配套。

这不是 `virt/kvm` 或 `arch/x86/kvm/vmx` 中的宿主侧 VMX 行为变更，
与 OVO 补丁没有 hunk 冲突。它仅在 CachyOS 本身运行在 KVM 虚拟机中时
参与 guest 侧 PV TLB shootdown，对普通裸机宿主上的 OVO VMX 路径无直接影响。

## 对当前 Intel OVO 补丁的影响矩阵

| 上游变化 | 移植状态 | 剩余风险/验证点 |
|---|---|---|
| mediated PMU 能力和非 const PMU ops | 已保留 7.1.3 API，OVO synthetic cap 设 `mediated=0` | 可作为未来重构基础，但原版 KVM 仍拒绝 hybrid capability |
| RDPMC intercept 动态重算 | 已在初始化和 recalc 两条路径无条件清除 exit bit | 用硬件计数和 VM-exit 观测确认 RDPMC 直通 |
| VMXON/VMXOFF 下沉及引用计数 | 已将 DS area 绑到 KVM get/put-ref 包装层 | 验证模块反复加载/卸载和 VMX-enable 失败清理 |
| IOAPIC 宏和内部结构整理 | 已保留新宏，重放 OVO pin/version 变更 | 验证 split/in-kernel irqchip、MSI 和高 pin 路由 |
| MMIO fragment 持久值 | 保留 7.1.3 的 `val`/`len` | 运行 ioeventfd/datamatch 和跨片 MMIO 测试 |
| CPUID 能力加固/新 Intel feature | OVO raw CPUID 仍独立于上游 policy | 验证每个绑定 vCPU 的 leaf 一致，尤其 AVX10/AMX/混合核 |
| shadow MMU、ioeventfd、Hyper-V 修复 | 未被补丁覆盖，已继承 | 覆盖 shadow paging、nested virtualization 和 Hyper-V enlightenment |

## 建议的最小运行时回归

1. 确认 `kvm`/`kvm_intel` 加载、卸载和重新加载时没有 DS area 泄漏、
   `WARN` 或 VMX refcount 错误。
2. 确认 `allow_hybrid_pmu=1`、P-core 集合和 vCPU affinity 如预期；单独检查
   `enable_mediated_pmu` 没有被意外组合启用。
3. 在 guest 内连续读 CPUID、RDPMC 和相关 PMU MSR，比较所有 vCPU 和重启前后
   的结果。
4. 运行含 ioeventfd/MMIO、shadow paging、nested VMX 和 Hyper-V enlightenment 的
   定向测试（仅测实际使用的功能）。
5. 检查 `dmesg` 中的 `KVM|VMX|VMCS|PMU|BUG|WARNING|objtool` 关键字，
   并对 VM-exit 快路径做长时间压力测试。

## 主要上游依据

- [Linux 7.0 official ChangeLog](https://cdn.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.0)
- [Linux 7.1 official ChangeLog](https://cdn.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.1)
- [Linux 7.1.3 official ChangeLog](https://cdn.kernel.org/pub/linux/kernel/v7.x/ChangeLog-7.1.3)
- [CachyOS Linux 7.1.3-1 release and applied branches](https://github.com/CachyOS/linux/releases/tag/cachyos-7.1.3-1)
- [7.0 KVM main pull (`cb5573868ea8`)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=cb5573868ea85ddbc74dd9a917acd1e434d21390)
- [7.0 mediated PMU pull (`bf2c3138ae36`)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=bf2c3138ae3694d4687cbe451c774c288ae2ad06)
- [7.0 APIC pull (`1b13885edf0a`)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=1b13885edf0a55a451a26d5fa53e7877b31debb5)
- [7.0 VMX pull (`687603fb2bf1`)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=687603fb2bf1205d6f7028e30848434e3b126a7a)
- [7.1 KVM main pull (`01f492e1817e`)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=01f492e1817e858d1712f2489d0afbaa552f417b)
- [7.1 VMXON/x86-virt pull (`4a530993dafe`)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=4a530993dafec27085321424aeab303eb0e7869e)
- [7.1 emulated-MMIO pull (`aa856775be63`)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=aa856775be633b00f4f535ce6d2ce0e6ae5ecb2f)
