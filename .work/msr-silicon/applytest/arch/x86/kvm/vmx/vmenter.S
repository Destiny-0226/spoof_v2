/* SPDX-License-Identifier: GPL-2.0 */
#include <linux/linkage.h>
#include <asm/asm.h>
#include <asm/bitsperlong.h>
#include <asm/kvm_vcpu_regs.h>
#include <asm/nospec-branch.h>
#include <asm/percpu.h>
#include <asm/segment.h>
#include "kvm-asm-offsets.h"
#include "run_flags.h"

#define WORD_SIZE (BITS_PER_LONG / 8)

#ifdef CONFIG_X86_64
/* Profiling is sampled and opt-in; each mark preserves guest registers. */
/* Store cumulative endpoints and commit only a complete direct hit. */
.macro CPUID_PROFILE_MARK tsc_field, flags_off, vmx_off
	testl $VMX_RUN_CPUID_PROFILE_SAMPLE, (\flags_off)(%_ASM_SP)
	jz .Lcpuid_profile_skip_\@
	push %_ASM_AX
	push %_ASM_DX
	push %_ASM_DI
	lfence
	rdtsc
	shl $32, %_ASM_DX
	or %_ASM_DX, %_ASM_AX
	mov (\vmx_off+3*WORD_SIZE)(%_ASM_SP), %_ASM_DI
	sub VMX_cpuid_profile_tsc_start(%_ASM_DI), %_ASM_AX
	mov %_ASM_AX, \tsc_field(%_ASM_DI)
	pop %_ASM_DI
	pop %_ASM_DX
	pop %_ASM_AX
.Lcpuid_profile_skip_\@:
.endm

#define CPUID_PROFILE_MARK_VALIDATION(flags_off, vmx_off) \
	CPUID_PROFILE_MARK VMX_cpuid_profile_tsc_validation, flags_off, vmx_off
#define CPUID_PROFILE_MARK_LOOKUP(flags_off, vmx_off) \
	CPUID_PROFILE_MARK VMX_cpuid_profile_tsc_lookup, flags_off, vmx_off
#define CPUID_PROFILE_MARK_RIP(flags_off, vmx_off) \
	CPUID_PROFILE_MARK VMX_cpuid_profile_tsc_rip, flags_off, vmx_off
#define CPUID_PROFILE_MARK_RESUME(flags_off, vmx_off) \
	CPUID_PROFILE_MARK VMX_cpuid_profile_tsc_resume, flags_off, vmx_off

.macro CPUID_PROFILE_COMMIT flags_off, vmx_off
	testl $VMX_RUN_CPUID_PROFILE_SAMPLE, (\flags_off)(%_ASM_SP)
	jz .Lcpuid_profile_commit_skip_\@
	push %_ASM_AX
	push %_ASM_DX
	push %_ASM_DI
	mov (\vmx_off+3*WORD_SIZE)(%_ASM_SP), %_ASM_DI
	mov VMX_cpuid_profile_tsc_validation(%_ASM_DI), %_ASM_AX
	add %_ASM_AX, VMX_stat_cpuid_profile_validation_cycles(%_ASM_DI)
	mov VMX_cpuid_profile_tsc_lookup(%_ASM_DI), %_ASM_AX
	add %_ASM_AX, VMX_stat_cpuid_profile_lookup_cycles(%_ASM_DI)
	mov VMX_cpuid_profile_tsc_rip(%_ASM_DI), %_ASM_AX
	add %_ASM_AX, VMX_stat_cpuid_profile_rip_cycles(%_ASM_DI)
	mov VMX_cpuid_profile_tsc_resume(%_ASM_DI), %_ASM_AX
	add %_ASM_AX, VMX_stat_cpuid_profile_resume_cycles(%_ASM_DI)
	incq VMX_stat_cpuid_profile_samples(%_ASM_DI)
	pop %_ASM_DI
	pop %_ASM_DX
	pop %_ASM_AX
.Lcpuid_profile_commit_skip_\@:
.endm

/* Measure from a sampled direct VMRESUME to the following VMEXIT boundary. */
.macro CPUID_PROFILE_TRANSITION_CAPTURE flags_off, vmx_off
	testl $VMX_RUN_CPUID_PROFILE, (\flags_off)(%_ASM_SP)
	jz .Lcpuid_profile_transition_capture_skip_\@
	push %_ASM_AX
	push %_ASM_DX
	push %_ASM_DI
	mov (\vmx_off+3*WORD_SIZE)(%_ASM_SP), %_ASM_DI
	movl $0, VMX_cpuid_profile_transition_ready(%_ASM_DI)
	cmpl $0, VMX_cpuid_profile_transition_armed(%_ASM_DI)
	je .Lcpuid_profile_transition_capture_restore_\@
	movl $0, VMX_cpuid_profile_transition_armed(%_ASM_DI)
	lfence
	rdtsc
	shl $32, %_ASM_DX
	or %_ASM_DX, %_ASM_AX
	sub VMX_cpuid_profile_transition_tsc_start(%_ASM_DI), %_ASM_AX
	mov %_ASM_AX, VMX_cpuid_profile_transition_elapsed(%_ASM_DI)
	movl $1, VMX_cpuid_profile_transition_ready(%_ASM_DI)
.Lcpuid_profile_transition_capture_restore_\@:
	pop %_ASM_DI
	pop %_ASM_DX
	pop %_ASM_AX
.Lcpuid_profile_transition_capture_skip_\@:
.endm

.macro CPUID_PROFILE_TRANSITION_COMMIT flags_off, vmx_off
	testl $VMX_RUN_CPUID_PROFILE, (\flags_off)(%_ASM_SP)
	jz .Lcpuid_profile_transition_commit_skip_\@
	push %_ASM_AX
	push %_ASM_DX
	push %_ASM_DI
	mov (\vmx_off+3*WORD_SIZE)(%_ASM_SP), %_ASM_DI
	cmpl $0, VMX_cpuid_profile_transition_ready(%_ASM_DI)
	je .Lcpuid_profile_transition_commit_restore_\@
	movl $0, VMX_cpuid_profile_transition_ready(%_ASM_DI)
	mov VMX_cpuid_profile_transition_elapsed(%_ASM_DI), %_ASM_AX
	add %_ASM_AX, VMX_stat_cpuid_profile_transition_cycles(%_ASM_DI)
	mov VMX_stat_cpuid_profile_transition_min_cycles(%_ASM_DI), %_ASM_DX
	test %_ASM_DX, %_ASM_DX
	jz .Lcpuid_profile_transition_set_min_\@
	cmp %_ASM_DX, %_ASM_AX
	jae .Lcpuid_profile_transition_min_done_\@
.Lcpuid_profile_transition_set_min_\@:
	mov %_ASM_AX, VMX_stat_cpuid_profile_transition_min_cycles(%_ASM_DI)
.Lcpuid_profile_transition_min_done_\@:
	mov VMX_stat_cpuid_profile_transition_max_cycles(%_ASM_DI), %_ASM_DX
	cmp %_ASM_DX, %_ASM_AX
	jbe .Lcpuid_profile_transition_max_done_\@
	mov %_ASM_AX, VMX_stat_cpuid_profile_transition_max_cycles(%_ASM_DI)
.Lcpuid_profile_transition_max_done_\@:
	incq VMX_stat_cpuid_profile_transition_samples(%_ASM_DI)
.Lcpuid_profile_transition_commit_restore_\@:
	pop %_ASM_DI
	pop %_ASM_DX
	pop %_ASM_AX
.Lcpuid_profile_transition_commit_skip_\@:
.endm

.macro CPUID_PROFILE_TRANSITION_ARM flags_off, vmx_off
	testl $VMX_RUN_CPUID_PROFILE_SAMPLE, (\flags_off)(%_ASM_SP)
	jz .Lcpuid_profile_transition_arm_skip_\@
	push %_ASM_AX
	push %_ASM_DX
	push %_ASM_DI
	mov (\vmx_off+3*WORD_SIZE)(%_ASM_SP), %_ASM_DI
	movl $0, VMX_cpuid_profile_transition_ready(%_ASM_DI)
	lfence
	rdtsc
	shl $32, %_ASM_DX
	or %_ASM_DX, %_ASM_AX
	mov %_ASM_AX, VMX_cpuid_profile_transition_tsc_start(%_ASM_DI)
	movl $1, VMX_cpuid_profile_transition_armed(%_ASM_DI)
	pop %_ASM_DI
	pop %_ASM_DX
	pop %_ASM_AX
.Lcpuid_profile_transition_arm_skip_\@:
.endm
#endif

#define VCPU_RAX	__VCPU_REGS_RAX * WORD_SIZE
#define VCPU_RCX	__VCPU_REGS_RCX * WORD_SIZE
#define VCPU_RDX	__VCPU_REGS_RDX * WORD_SIZE
#define VCPU_RBX	__VCPU_REGS_RBX * WORD_SIZE
/* Intentionally omit RSP as it's context switched by hardware */
#define VCPU_RBP	__VCPU_REGS_RBP * WORD_SIZE
#define VCPU_RSI	__VCPU_REGS_RSI * WORD_SIZE
#define VCPU_RDI	__VCPU_REGS_RDI * WORD_SIZE

#ifdef CONFIG_X86_64
#define VCPU_R8		__VCPU_REGS_R8  * WORD_SIZE
#define VCPU_R9		__VCPU_REGS_R9  * WORD_SIZE
#define VCPU_R10	__VCPU_REGS_R10 * WORD_SIZE
#define VCPU_R11	__VCPU_REGS_R11 * WORD_SIZE
#define VCPU_R12	__VCPU_REGS_R12 * WORD_SIZE
#define VCPU_R13	__VCPU_REGS_R13 * WORD_SIZE
#define VCPU_R14	__VCPU_REGS_R14 * WORD_SIZE
#define VCPU_R15	__VCPU_REGS_R15 * WORD_SIZE
#endif

.section .noinstr.text, "ax"

/**
 * __vmx_vcpu_run - Run a vCPU via a transition to VMX guest mode
 * @vmx:	struct vcpu_vmx *
 * @regs:	unsigned long * (to guest registers)
 * @flags:	VMX_RUN_VMRESUME:	use VMRESUME instead of VMLAUNCH
 *		VMX_RUN_SAVE_SPEC_CTRL: save guest SPEC_CTRL into vmx->spec_ctrl
 *		VMX_RUN_CLEAR_CPU_BUFFERS_FOR_MMIO: vCPU can access host MMIO
 *
 * Returns:
 *	0 on VM-Exit, 1 on VM-Fail
 */
SYM_FUNC_START(__vmx_vcpu_run)
	push %_ASM_BP
	mov  %_ASM_SP, %_ASM_BP
#ifdef CONFIG_X86_64
	push %r15
	push %r14
	push %r13
	push %r12
#else
	push %edi
	push %esi
#endif
	push %_ASM_BX

	/* Save @vmx for SPEC_CTRL handling */
	push %_ASM_ARG1

	/* Save @flags (used for VMLAUNCH vs. VMRESUME and mitigations). */
	push %_ASM_ARG3

	/*
	 * Save @regs, _ASM_ARG2 may be modified by vmx_update_host_rsp() and
	 * @regs is needed after VM-Exit to save the guest's register values.
	 */
	push %_ASM_ARG2

	lea (%_ASM_SP), %_ASM_ARG2
	call vmx_update_host_rsp

	ALTERNATIVE "jmp .Lspec_ctrl_done", "", X86_FEATURE_MSR_SPEC_CTRL

	/*
	 * SPEC_CTRL handling: if the guest's SPEC_CTRL value differs from the
	 * host's, write the MSR.
	 *
	 * IMPORTANT: To avoid RSB underflow attacks and any other nastiness,
	 * there must not be any returns or indirect branches between this code
	 * and vmentry.
	 */
	mov 2*WORD_SIZE(%_ASM_SP), %_ASM_DI
#ifdef CONFIG_X86_64
	mov VMX_spec_ctrl(%rdi), %rdx
	cmp PER_CPU_VAR(x86_spec_ctrl_current), %rdx
	je .Lspec_ctrl_done
	movl %edx, %eax
	shr $32, %rdx
#else
	mov VMX_spec_ctrl(%edi), %eax
	mov PER_CPU_VAR(x86_spec_ctrl_current), %ecx
	xor %eax, %ecx
	mov VMX_spec_ctrl + 4(%edi), %edx
	mov PER_CPU_VAR(x86_spec_ctrl_current + 4), %edi
	xor %edx, %edi
	or %edi, %ecx
	je .Lspec_ctrl_done
#endif
	mov $MSR_IA32_SPEC_CTRL, %ecx
	wrmsr

.Lspec_ctrl_done:

	/*
	 * Since vmentry is serializing on affected CPUs, there's no need for
	 * an LFENCE to stop speculation from skipping the wrmsr.
	 */

	/* Load @regs to RAX. */
	mov (%_ASM_SP), %_ASM_AX

	/* Load guest registers.  Don't clobber flags. */
	mov VCPU_RCX(%_ASM_AX), %_ASM_CX
	mov VCPU_RDX(%_ASM_AX), %_ASM_DX
	mov VCPU_RBX(%_ASM_AX), %_ASM_BX
	mov VCPU_RBP(%_ASM_AX), %_ASM_BP
	mov VCPU_RSI(%_ASM_AX), %_ASM_SI
	mov VCPU_RDI(%_ASM_AX), %_ASM_DI
#ifdef CONFIG_X86_64
	mov VCPU_R8 (%_ASM_AX),  %r8
	mov VCPU_R9 (%_ASM_AX),  %r9
	mov VCPU_R10(%_ASM_AX), %r10
	mov VCPU_R11(%_ASM_AX), %r11
	mov VCPU_R12(%_ASM_AX), %r12
	mov VCPU_R13(%_ASM_AX), %r13
	mov VCPU_R14(%_ASM_AX), %r14
	mov VCPU_R15(%_ASM_AX), %r15
#endif
	/* Load guest RAX.  This kills the @regs pointer! */
	mov VCPU_RAX(%_ASM_AX), %_ASM_AX

	/*
	 * Note, ALTERNATIVE_2 works in reverse order.  If CLEAR_CPU_BUF_VM is
	 * enabled, do VERW unconditionally.  If CPU_BUF_VM_MMIO is enabled,
	 * check @flags to see if the vCPU has access to host MMIO, and if so,
	 * do VERW.  Else, do nothing (no mitigations needed/enabled).
	 */
	ALTERNATIVE_2 "",									  \
		      __stringify(testl $VMX_RUN_CLEAR_CPU_BUFFERS_FOR_MMIO, WORD_SIZE(%_ASM_SP); \
				  jz .Lskip_mmio_verw;						  \
				  VERW;								  \
				  .Lskip_mmio_verw:),					  	  \
		      X86_FEATURE_CLEAR_CPU_BUF_VM_MMIO,					  \
		      __stringify(VERW), X86_FEATURE_CLEAR_CPU_BUF_VM

	/* Check @flags to see if VMLAUNCH or VMRESUME is needed. */
	testl $VMX_RUN_VMRESUME, WORD_SIZE(%_ASM_SP)
	jz .Lvmlaunch

	/*
	 * After a successful VMRESUME/VMLAUNCH, control flow "magically"
	 * resumes below at 'vmx_vmexit' due to the VMCS HOST_RIP setting.
	 * So this isn't a typical function and objtool needs to be told to
	 * save the unwind state here and restore it below.
	 */
	UNWIND_HINT_SAVE

/*
 * If VMRESUME/VMLAUNCH and corresponding vmexit succeed, execution resumes at
 * the 'vmx_vmexit' label below.
 */
.Lvmresume:
	vmresume
	jmp .Lvmfail

.Lvmlaunch:
	vmlaunch
	jmp .Lvmfail

	_ASM_EXTABLE(.Lvmresume, .Lfixup)
	_ASM_EXTABLE(.Lvmlaunch, .Lfixup)

SYM_INNER_LABEL_ALIGN(vmx_vmexit, SYM_L_GLOBAL)

	/* Restore unwind state from before the VMRESUME/VMLAUNCH. */
	UNWIND_HINT_RESTORE
	ENDBR

#ifdef CONFIG_X86_64
	CPUID_PROFILE_TRANSITION_CAPTURE WORD_SIZE, 2*WORD_SIZE
	/* Sample one eligible VMEXIT every 1024 exits for stage accounting. */
	andl $~VMX_RUN_CPUID_PROFILE_SAMPLE, WORD_SIZE(%_ASM_SP)
	testl $VMX_RUN_CPUID_PROFILE, WORD_SIZE(%_ASM_SP)
	jz .Lcpuid_profile_unarmed
	push %_ASM_DI
	mov 3*WORD_SIZE(%_ASM_SP), %_ASM_DI
	cmpl $0, VMX_cpuid_profile_divider(%_ASM_DI)
	je .Lcpuid_profile_take_sample
	decl VMX_cpuid_profile_divider(%_ASM_DI)
	jnz .Lcpuid_profile_pop_di

.Lcpuid_profile_take_sample:
	movl $1024, VMX_cpuid_profile_divider(%_ASM_DI)
	push %_ASM_AX
	push %_ASM_DX
	lfence
	rdtsc
	shl $32, %_ASM_DX
	or %_ASM_DX, %_ASM_AX
	mov %_ASM_AX, VMX_cpuid_profile_tsc_start(%_ASM_DI)
	orl $VMX_RUN_CPUID_PROFILE_SAMPLE, 4*WORD_SIZE(%_ASM_SP)
	pop %_ASM_DX
	pop %_ASM_AX
.Lcpuid_profile_pop_di:
	pop %_ASM_DI
.Lcpuid_profile_unarmed:

	/*
	 * Try the direct-mapped home slot before spilling the complete guest
	 * register file.  CPUID overwrites RAX/RBX/RCX/RDX, so after the exit
	 * and event state are validated those four registers are available as
	 * scratch.  All other guest GPRs remain live until VMRESUME.
	 */
	testl $VMX_RUN_CPUID_INNER_EARLY, WORD_SIZE(%_ASM_SP)
	jz .Lcpuid_inner_save_regs

	/*
	 * RBX and RDX are needed to identify and validate the exit.  Preserve
	 * them until it is known that CPUID can be completed immediately.
	 */
	push %_ASM_BX
	push %_ASM_DX

	mov $0x4402, %ebx		/* VM_EXIT_REASON */
	vmread %_ASM_BX, %_ASM_DX
	jbe .Lcpuid_inner_early_restore
	cmp $0x0a, %edx			/* EXIT_REASON_CPUID, no extra flags */
	jne .Lcpuid_inner_early_restore
	CPUID_PROFILE_TRANSITION_COMMIT 3*WORD_SIZE, 4*WORD_SIZE

	mov $0x4408, %ebx		/* IDT_VECTORING_INFO_FIELD */
	vmread %_ASM_BX, %_ASM_DX
	jbe .Lcpuid_inner_early_restore
	test $0x80000000, %edx
	jnz .Lcpuid_inner_early_restore

	mov $0x4824, %ebx		/* GUEST_INTERRUPTIBILITY_INFO */
	vmread %_ASM_BX, %_ASM_DX
	jbe .Lcpuid_inner_early_restore
	test $0x03, %edx		/* STI or MOV SS shadow */
	jnz .Lcpuid_inner_early_restore

	mov $0x6820, %ebx		/* GUEST_RFLAGS */
	vmread %_ASM_BX, %_ASM_DX
	jbe .Lcpuid_inner_early_restore
	test $0x100, %edx		/* X86_EFLAGS_TF */
	jnz .Lcpuid_inner_early_restore
	CPUID_PROFILE_MARK_VALIDATION(3*WORD_SIZE, 4*WORD_SIZE)

	/* CPUID will replace RBX and RDX, so their input values are dead. */
	add $2*WORD_SIZE, %_ASM_SP

	/* Reuse the last exact cache entry before recomputing its hash. */
	mov 2*WORD_SIZE(%_ASM_SP), %_ASM_DX
	mov VMX_cpuid_fast_mru(%_ASM_DX), %_ASM_BX
	test %_ASM_BX, %_ASM_BX
	jz .Lcpuid_inner_early_nonindexed
	mov VMX_CPUID_CACHE_flags(%_ASM_BX), %edx
	test $VMX_CPUID_CACHE_valid, %edx
	jz .Lcpuid_inner_early_nonindexed
	cmp %eax, VMX_CPUID_CACHE_function(%_ASM_BX)
	jne .Lcpuid_inner_early_nonindexed
	test $VMX_CPUID_CACHE_indexed, %edx
	jz .Lcpuid_inner_early_hit
	cmp %ecx, VMX_CPUID_CACHE_index(%_ASM_BX)
	je .Lcpuid_inner_early_hit

.Lcpuid_inner_early_nonindexed:
	/* First try the non-indexed direct-map slot. */
	mov %eax, %ebx
	imul $0x9e3779b1, %ebx, %ebx
	shr $24, %ebx
	and $VMX_CPUID_CACHE_mask, %ebx
	imul $VMX_CPUID_CACHE_entry_size, %ebx, %ebx
	mov 2*WORD_SIZE(%_ASM_SP), %_ASM_DX
	lea VMX_cpuid_fast_cache(%_ASM_DX, %_ASM_BX), %_ASM_BX
	mov VMX_CPUID_CACHE_flags(%_ASM_BX), %edx
	test $VMX_CPUID_CACHE_valid, %edx
	jz .Lcpuid_inner_early_indexed
	test $VMX_CPUID_CACHE_indexed, %edx
	jnz .Lcpuid_inner_early_indexed
	cmp %eax, VMX_CPUID_CACHE_function(%_ASM_BX)
	je .Lcpuid_inner_early_cache_hit

.Lcpuid_inner_early_indexed:
	/* Then try the exact indexed direct-map slot. */
	mov %eax, %ebx
	imul $0x9e3779b1, %ebx, %ebx
	mov %ecx, %edx
	imul $0x85ebca6b, %edx, %edx
	xor %edx, %ebx
	shr $24, %ebx
	and $VMX_CPUID_CACHE_mask, %ebx
	imul $VMX_CPUID_CACHE_entry_size, %ebx, %ebx
	mov 2*WORD_SIZE(%_ASM_SP), %_ASM_DX
	lea VMX_cpuid_fast_cache(%_ASM_DX, %_ASM_BX), %_ASM_BX
	mov VMX_CPUID_CACHE_flags(%_ASM_BX), %edx
	test $VMX_CPUID_CACHE_valid, %edx
	jz .Lcpuid_inner_early_cache_miss
	test $VMX_CPUID_CACHE_indexed, %edx
	jz .Lcpuid_inner_early_cache_miss
	cmp %eax, VMX_CPUID_CACHE_function(%_ASM_BX)
	jne .Lcpuid_inner_early_cache_miss
	cmp %ecx, VMX_CPUID_CACHE_index(%_ASM_BX)
	jne .Lcpuid_inner_early_cache_miss

.Lcpuid_inner_early_cache_hit:
	mov 2*WORD_SIZE(%_ASM_SP), %_ASM_DX
	mov %_ASM_BX, VMX_cpuid_fast_mru(%_ASM_DX)
	jmp .Lcpuid_inner_early_hit

.Lcpuid_inner_early_cache_miss:
	/*
	 * Tell the post-spill path that all VMCS safety checks already passed,
	 * avoiding a second set of VMREADs for collisions and dynamic leaves.
	 */
	orl $VMX_RUN_CPUID_INNER_EARLY_VALIDATED, WORD_SIZE(%_ASM_SP)
	jmp .Lcpuid_inner_save_regs

.Lcpuid_inner_early_hit:
	/*
	 * Preserve the CPUID inputs and cache pointer until every VMCS access
	 * succeeds.  This keeps the ordinary slow path recoverable even on an
	 * unexpected VMREAD/VMWRITE failure.
	 */
	push %_ASM_AX
	push %_ASM_CX
	push %_ASM_BX
	CPUID_PROFILE_MARK_LOOKUP(4*WORD_SIZE, 5*WORD_SIZE)

	mov $0x440c, %eax		/* VM_EXIT_INSTRUCTION_LEN */
	vmread %_ASM_AX, %_ASM_CX
	jbe .Lcpuid_inner_early_hit_fail
	mov $0x681e, %eax		/* GUEST_RIP */
	vmread %_ASM_AX, %_ASM_DX
	jbe .Lcpuid_inner_early_hit_fail

	mov %_ASM_DX, %_ASM_BX
	add %_ASM_CX, %_ASM_DX
	mov %_ASM_DX, %_ASM_CX
	xor %_ASM_BX, %_ASM_CX
	shr $31, %_ASM_CX
	cmp $3, %_ASM_CX
	jne .Lcpuid_inner_early_write_rip
	mov $0x4816, %eax		/* GUEST_CS_AR_BYTES */
	vmread %_ASM_AX, %_ASM_CX
	jbe .Lcpuid_inner_early_hit_fail
	test $0x2000, %ecx		/* VMX_AR_L_MASK */
	jnz .Lcpuid_inner_early_write_rip
	mov %edx, %edx			/* 32-bit RIP wraps at bit 31. */

.Lcpuid_inner_early_write_rip:
	mov $0x681e, %eax
	vmwrite %_ASM_DX, %_ASM_AX
	jbe .Lcpuid_inner_early_hit_fail
	CPUID_PROFILE_MARK_RIP(4*WORD_SIZE, 5*WORD_SIZE)

	pop %_ASM_BX			/* Cache entry. */
	add $2*WORD_SIZE, %_ASM_SP	/* Discard saved RCX and RAX. */

	/* Complete accounting before loading the four architectural outputs. */
	mov 2*WORD_SIZE(%_ASM_SP), %_ASM_DX
	incq VMX_stat_exits(%_ASM_DX)
	incq VMX_stat_cpuid_fastpath(%_ASM_DX)
	incq VMX_stat_cpuid_inner_fastpath(%_ASM_DX)
	mov VMX_loaded_vmcs(%_ASM_DX), %_ASM_AX
	movb $1, LOADED_VMCS_launched(%_ASM_AX)

	/* Keep the canonical array synchronized for VMRESUME failure handling. */
	push %_ASM_DI
	mov WORD_SIZE(%_ASM_SP), %_ASM_DI
	mov VMX_CPUID_CACHE_eax(%_ASM_BX), %eax
	mov %_ASM_AX, VCPU_RAX(%_ASM_DI)
	mov VMX_CPUID_CACHE_ecx(%_ASM_BX), %ecx
	mov %_ASM_CX, VCPU_RCX(%_ASM_DI)
	mov VMX_CPUID_CACHE_edx(%_ASM_BX), %edx
	mov %_ASM_DX, VCPU_RDX(%_ASM_DI)
	mov VMX_CPUID_CACHE_ebx(%_ASM_BX), %ebx
	mov %_ASM_BX, VCPU_RBX(%_ASM_DI)
	pop %_ASM_DI

	/* Replay timing4's guarded VM-entry buffer clearing policy. */
	ALTERNATIVE_2 "",\
		      __stringify(testl $VMX_RUN_CLEAR_CPU_BUFFERS_FOR_MMIO, WORD_SIZE(%_ASM_SP); \
				  jz .Lcpuid_inner_early_skip_mmio_verw; \
				  VERW; \
				  .Lcpuid_inner_early_skip_mmio_verw:), \
		      X86_FEATURE_CLEAR_CPU_BUF_VM_MMIO, \
		      __stringify(testl $VMX_RUN_CPUID_INNER_SKIP_RFDS_VERW, WORD_SIZE(%_ASM_SP); \
				  jnz .Lcpuid_inner_early_skip_rfds_verw; \
				  VERW; \
				  .Lcpuid_inner_early_skip_rfds_verw:), \
			      X86_FEATURE_CLEAR_CPU_BUF_VM
	CPUID_PROFILE_MARK_RESUME(WORD_SIZE, 2*WORD_SIZE)
	CPUID_PROFILE_COMMIT WORD_SIZE, 2*WORD_SIZE
	CPUID_PROFILE_TRANSITION_ARM WORD_SIZE, 2*WORD_SIZE
	jmp .Lvmresume

.Lcpuid_inner_early_hit_fail:
	add $WORD_SIZE, %_ASM_SP	/* Discard the cache entry. */
	pop %_ASM_CX
	pop %_ASM_AX
	orl $VMX_RUN_CPUID_INNER_EARLY_VALIDATED, WORD_SIZE(%_ASM_SP)
	jmp .Lcpuid_inner_save_regs

.Lcpuid_inner_early_restore:
	pop %_ASM_DX
	pop %_ASM_BX
	orl $VMX_RUN_CPUID_INNER_EARLY_REJECTED, WORD_SIZE(%_ASM_SP)

.Lcpuid_inner_save_regs:
#endif

	/* Temporarily save guest's RAX. */
	push %_ASM_AX

	/* Reload @regs to RAX. */
	mov WORD_SIZE(%_ASM_SP), %_ASM_AX

	/* Save all guest registers, including RAX from the stack */
	pop           VCPU_RAX(%_ASM_AX)
	mov %_ASM_CX, VCPU_RCX(%_ASM_AX)
	mov %_ASM_DX, VCPU_RDX(%_ASM_AX)
	mov %_ASM_BX, VCPU_RBX(%_ASM_AX)
	mov %_ASM_BP, VCPU_RBP(%_ASM_AX)
	mov %_ASM_SI, VCPU_RSI(%_ASM_AX)
	mov %_ASM_DI, VCPU_RDI(%_ASM_AX)
#ifdef CONFIG_X86_64
	mov %r8,  VCPU_R8 (%_ASM_AX)
	mov %r9,  VCPU_R9 (%_ASM_AX)
	mov %r10, VCPU_R10(%_ASM_AX)
	mov %r11, VCPU_R11(%_ASM_AX)
	mov %r12, VCPU_R12(%_ASM_AX)
	mov %r13, VCPU_R13(%_ASM_AX)
	mov %r14, VCPU_R14(%_ASM_AX)
	mov %r15, VCPU_R15(%_ASM_AX)
#endif

#ifdef CONFIG_X86_64
	/*
	 * Complete immutable CPUID leaves without returning to the C run loop.
	 * C enables this only when debug, PMU, tracing, nested state, and active
	 * entry mitigations permit an immediate re-entry.
	 */
	testl $VMX_RUN_CPUID_INNER_EARLY_REJECTED, WORD_SIZE(%_ASM_SP)
	jnz .Lcpuid_inner_early_rejected_saved
	testl $VMX_RUN_CPUID_INNER_EARLY_VALIDATED, WORD_SIZE(%_ASM_SP)
	jnz .Lcpuid_inner_early_validated_saved

	testl $VMX_RUN_CPUID_INNER_FASTPATH, WORD_SIZE(%_ASM_SP)
	jz .Lcpuid_inner_slowpath

	mov $0x4402, %esi		/* VM_EXIT_REASON */
	vmread %_ASM_SI, %_ASM_DX
	jbe .Lcpuid_inner_slowpath
	cmp $0x0a, %edx			/* EXIT_REASON_CPUID, no extra flags */
	jne .Lcpuid_inner_slowpath
	CPUID_PROFILE_TRANSITION_COMMIT WORD_SIZE, 2*WORD_SIZE

	mov $0x4408, %esi		/* IDT_VECTORING_INFO_FIELD */
	vmread %_ASM_SI, %_ASM_DX
	jbe .Lcpuid_inner_slowpath
	test $0x80000000, %edx
	jnz .Lcpuid_inner_slowpath

	mov $0x4824, %esi		/* GUEST_INTERRUPTIBILITY_INFO */
	vmread %_ASM_SI, %_ASM_DX
	jbe .Lcpuid_inner_slowpath
	test $0x03, %edx		/* STI or MOV SS shadow */
	jnz .Lcpuid_inner_slowpath

	mov $0x6820, %esi		/* GUEST_RFLAGS */
	vmread %_ASM_SI, %_ASM_DX
	jbe .Lcpuid_inner_slowpath
	test $0x100, %edx		/* X86_EFLAGS_TF */
	jnz .Lcpuid_inner_slowpath

.Lcpuid_inner_lookup:
	CPUID_PROFILE_MARK_VALIDATION(WORD_SIZE, 2*WORD_SIZE)
	mov %_ASM_AX, %_ASM_DI		/* &vcpu->arch.regs */
	mov 2*WORD_SIZE(%_ASM_SP), %_ASM_BX	/* struct vcpu_vmx * */
	mov VCPU_RAX(%_ASM_DI), %r8d	/* function */
	mov VCPU_RCX(%_ASM_DI), %r9d	/* input index */
	xor %r10d, %r10d		/* First try a non-indexed entry. */

.Lcpuid_inner_hash:
	mov %r8d, %edx
	imul $0x9e3779b1, %edx, %edx
	test %r10d, %r10d
	jz .Lcpuid_inner_hash_ready
	mov %r9d, %ecx
	imul $0x85ebca6b, %ecx, %ecx
	xor %ecx, %edx

.Lcpuid_inner_hash_ready:
	/* Use high product bits; low CPUID bytes are heavily clustered. */
	shr $24, %edx
	/* The cache has 256 slots, so the high byte is the final bucket. */
	and $VMX_CPUID_CACHE_mask, %edx
	mov %edx, %r11d
	mov $VMX_CPUID_CACHE_probes, %ebp

.Lcpuid_inner_probe:
	mov %r11d, %edx
	and $VMX_CPUID_CACHE_mask, %edx
	imul $VMX_CPUID_CACHE_entry_size, %edx, %edx
	lea VMX_cpuid_fast_cache(%_ASM_BX, %_ASM_DX), %_ASM_SI
	mov VMX_CPUID_CACHE_flags(%_ASM_SI), %ecx
	test $VMX_CPUID_CACHE_valid, %ecx
	jz .Lcpuid_inner_next_mode
	and $VMX_CPUID_CACHE_indexed, %ecx
	cmp %r10d, %ecx
	jne .Lcpuid_inner_next_slot
	cmp %r8d, VMX_CPUID_CACHE_function(%_ASM_SI)
	jne .Lcpuid_inner_next_slot
	test %r10d, %r10d
	jz .Lcpuid_inner_hit
	cmp %r9d, VMX_CPUID_CACHE_index(%_ASM_SI)
	je .Lcpuid_inner_hit

.Lcpuid_inner_next_slot:
	inc %r11d
	dec %ebp
	jnz .Lcpuid_inner_probe

.Lcpuid_inner_next_mode:
	test %r10d, %r10d
	jnz .Lcpuid_inner_cache_miss
	mov $VMX_CPUID_CACHE_indexed, %r10d
	jmp .Lcpuid_inner_hash

.Lcpuid_inner_cache_miss:
	incq VMX_stat_cpuid_inner_cache_miss(%_ASM_BX)
	jmp .Lcpuid_inner_slowpath

.Lcpuid_inner_hit:
	mov %_ASM_SI, %r12		/* Preserve the cache entry. */
	mov %r12, VMX_cpuid_fast_mru(%_ASM_BX)
	CPUID_PROFILE_MARK_LOOKUP(WORD_SIZE, 2*WORD_SIZE)
	mov $0x440c, %esi		/* VM_EXIT_INSTRUCTION_LEN */
	vmread %_ASM_SI, %_ASM_CX
	jbe .Lcpuid_inner_slowpath
	mov $0x681e, %esi		/* GUEST_RIP */
	vmread %_ASM_SI, %_ASM_DX
	jbe .Lcpuid_inner_slowpath

	mov %_ASM_DX, %r11
	add %_ASM_CX, %_ASM_DX
	mov %_ASM_DX, %_ASM_CX
	xor %r11, %_ASM_CX
	shr $31, %_ASM_CX
	cmp $3, %_ASM_CX
	jne .Lcpuid_inner_write_rip
	mov $0x4816, %esi		/* GUEST_CS_AR_BYTES */
	vmread %_ASM_SI, %_ASM_CX
	jbe .Lcpuid_inner_slowpath
	test $0x2000, %ecx		/* VMX_AR_L_MASK */
	jnz .Lcpuid_inner_write_rip
	mov %edx, %edx			/* 32-bit RIP wraps at bit 31. */

.Lcpuid_inner_write_rip:
	mov $0x681e, %esi
	vmwrite %_ASM_DX, %_ASM_SI
	jbe .Lcpuid_inner_slowpath
	CPUID_PROFILE_MARK_RIP(WORD_SIZE, 2*WORD_SIZE)

	/* CPUID writes zero-extended 32-bit values to all four outputs. */
	mov VMX_CPUID_CACHE_eax(%r12), %edx
	mov %_ASM_DX, VCPU_RAX(%_ASM_DI)
	mov VMX_CPUID_CACHE_ebx(%r12), %edx
	mov %_ASM_DX, VCPU_RBX(%_ASM_DI)
	mov VMX_CPUID_CACHE_ecx(%r12), %edx
	mov %_ASM_DX, VCPU_RCX(%_ASM_DI)
	mov VMX_CPUID_CACHE_edx(%r12), %edx
	mov %_ASM_DX, VCPU_RDX(%_ASM_DI)

	incq VMX_stat_exits(%_ASM_BX)
	incq VMX_stat_cpuid_fastpath(%_ASM_BX)
	incq VMX_stat_cpuid_inner_fastpath(%_ASM_BX)
	mov VMX_loaded_vmcs(%_ASM_BX), %_ASM_DX
	movb $1, LOADED_VMCS_launched(%_ASM_DX)

	/* Reload the complete guest GPR set and resume the launched VMCS. */
	mov VCPU_R8 (%_ASM_DI), %r8
	mov VCPU_R9 (%_ASM_DI), %r9
	mov VCPU_R10(%_ASM_DI), %r10
	mov VCPU_R11(%_ASM_DI), %r11
	mov VCPU_R12(%_ASM_DI), %r12
	mov VCPU_R13(%_ASM_DI), %r13
	mov VCPU_R14(%_ASM_DI), %r14
	mov VCPU_R15(%_ASM_DI), %r15
	mov VCPU_RAX(%_ASM_DI), %_ASM_AX
	mov VCPU_RCX(%_ASM_DI), %_ASM_CX
	mov VCPU_RDX(%_ASM_DI), %_ASM_DX
	mov VCPU_RBX(%_ASM_DI), %_ASM_BX
	mov VCPU_RBP(%_ASM_DI), %_ASM_BP
	mov VCPU_RSI(%_ASM_DI), %_ASM_SI
	mov VCPU_RDI(%_ASM_DI), %_ASM_DI

	/* Replay the standard VM-entry buffer clearing before direct re-entry. */
	ALTERNATIVE_2 "",\
		      __stringify(testl $VMX_RUN_CLEAR_CPU_BUFFERS_FOR_MMIO, WORD_SIZE(%_ASM_SP); \
				  jz .Lcpuid_inner_skip_mmio_verw; \
				  VERW; \
				  .Lcpuid_inner_skip_mmio_verw:), \
		      X86_FEATURE_CLEAR_CPU_BUF_VM_MMIO, \
			      __stringify(testl $VMX_RUN_CPUID_INNER_SKIP_RFDS_VERW, WORD_SIZE(%_ASM_SP); \
				  jnz .Lcpuid_inner_skip_rfds_verw; \
				  VERW; \
				  .Lcpuid_inner_skip_rfds_verw:), \
			      X86_FEATURE_CLEAR_CPU_BUF_VM
	CPUID_PROFILE_MARK_RESUME(WORD_SIZE, 2*WORD_SIZE)
	CPUID_PROFILE_COMMIT WORD_SIZE, 2*WORD_SIZE
	CPUID_PROFILE_TRANSITION_ARM WORD_SIZE, 2*WORD_SIZE
	jmp .Lvmresume

.Lcpuid_inner_early_validated_saved:
	andl $~(VMX_RUN_CPUID_INNER_EARLY_VALIDATED | \
		 VMX_RUN_CPUID_INNER_EARLY_REJECTED), WORD_SIZE(%_ASM_SP)
	jmp .Lcpuid_inner_lookup

.Lcpuid_inner_early_rejected_saved:
	andl $~(VMX_RUN_CPUID_INNER_EARLY_VALIDATED | \
		 VMX_RUN_CPUID_INNER_EARLY_REJECTED), WORD_SIZE(%_ASM_SP)

.Lcpuid_inner_slowpath:
#endif

	/* Clear return value to indicate VM-Exit (as opposed to VM-Fail). */
	xor %ebx, %ebx

.Lclear_regs:
	/* Discard @regs.  The register is irrelevant, it just can't be RBX. */
	pop %_ASM_AX

	/*
	 * Clear all general purpose registers except RSP and RBX to prevent
	 * speculative use of the guest's values, even those that are reloaded
	 * via the stack.  In theory, an L1 cache miss when restoring registers
	 * could lead to speculative execution with the guest's values.
	 * Zeroing XORs are dirt cheap, i.e. the extra paranoia is essentially
	 * free.  RSP and RBX are exempt as RSP is restored by hardware during
	 * VM-Exit and RBX is explicitly loaded with 0 or 1 to hold the return
	 * value.
	 */
	xor %eax, %eax
	xor %ecx, %ecx
	xor %edx, %edx
	xor %ebp, %ebp
	xor %esi, %esi
	xor %edi, %edi
#ifdef CONFIG_X86_64
	xor %r8d,  %r8d
	xor %r9d,  %r9d
	xor %r10d, %r10d
	xor %r11d, %r11d
	xor %r12d, %r12d
	xor %r13d, %r13d
	xor %r14d, %r14d
	xor %r15d, %r15d
#endif

	/*
	 * IMPORTANT: RSB filling and SPEC_CTRL handling must be done before
	 * the first unbalanced RET after vmexit!
	 *
	 * For retpoline or IBRS, RSB filling is needed to prevent poisoned RSB
	 * entries and (in some cases) RSB underflow.
	 *
	 * eIBRS has its own protection against poisoned RSB, so it doesn't
	 * need the RSB filling sequence.  But it does need to be enabled, and a
	 * single call to retire, before the first unbalanced RET.
	 */

	FILL_RETURN_BUFFER %_ASM_CX, RSB_CLEAR_LOOPS, X86_FEATURE_RSB_VMEXIT,\
			   X86_FEATURE_RSB_VMEXIT_LITE

	pop %_ASM_ARG2	/* @flags */
	pop %_ASM_ARG1	/* @vmx */

	call vmx_spec_ctrl_restore_host

	CLEAR_BRANCH_HISTORY_VMEXIT

	/* Put return value in AX */
	mov %_ASM_BX, %_ASM_AX

	pop %_ASM_BX
#ifdef CONFIG_X86_64
	pop %r12
	pop %r13
	pop %r14
	pop %r15
#else
	pop %esi
	pop %edi
#endif
	pop %_ASM_BP
	RET

.Lfixup:
	cmpb $0, _ASM_RIP(virt_rebooting)
	jne .Lvmfail
	ud2
.Lvmfail:
	/* VM-Fail: set return value to 1 */
	mov $1, %_ASM_BX
	jmp .Lclear_regs

SYM_FUNC_END(__vmx_vcpu_run)

#ifndef CONFIG_CC_HAS_ASM_GOTO_OUTPUT

/**
 * vmread_error_trampoline - Trampoline from inline asm to vmread_error()
 * @field:	VMCS field encoding that failed
 * @fault:	%true if the VMREAD faulted, %false if it failed
 *
 * Save and restore volatile registers across a call to vmread_error().  Note,
 * all parameters are passed on the stack.
 */
SYM_FUNC_START(vmread_error_trampoline)
	push %_ASM_BP
	mov  %_ASM_SP, %_ASM_BP

	push %_ASM_AX
	push %_ASM_CX
	push %_ASM_DX
#ifdef CONFIG_X86_64
	push %rdi
	push %rsi
	push %r8
	push %r9
	push %r10
	push %r11
#endif

	/* Load @field and @fault to arg1 and arg2 respectively. */
	mov 3*WORD_SIZE(%_ASM_BP), %_ASM_ARG2
	mov 2*WORD_SIZE(%_ASM_BP), %_ASM_ARG1

	call vmread_error_trampoline2

	/* Zero out @fault, which will be popped into the result register. */
	_ASM_MOV $0, 3*WORD_SIZE(%_ASM_BP)

#ifdef CONFIG_X86_64
	pop %r11
	pop %r10
	pop %r9
	pop %r8
	pop %rsi
	pop %rdi
#endif
	pop %_ASM_DX
	pop %_ASM_CX
	pop %_ASM_AX
	pop %_ASM_BP

	RET
SYM_FUNC_END(vmread_error_trampoline)
#endif
