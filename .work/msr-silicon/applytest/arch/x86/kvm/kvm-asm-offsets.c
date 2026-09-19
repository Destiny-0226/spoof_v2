// SPDX-License-Identifier: GPL-2.0
/*
 * Generate definitions needed by assembly language modules.
 * This code generates raw asm output which is post-processed to extract
 * and format the required data.
 */
#define COMPILE_OFFSETS

#include <linux/kbuild.h>
#include "vmx/vmx.h"
#include "svm/svm.h"

static void __used common(void)
{
	if (IS_ENABLED(CONFIG_KVM_AMD)) {
		BLANK();
		OFFSET(SVM_vcpu_arch_regs, vcpu_svm, vcpu.arch.regs);
		OFFSET(SVM_current_vmcb, vcpu_svm, current_vmcb);
		OFFSET(SVM_spec_ctrl, vcpu_svm, spec_ctrl);
		OFFSET(SVM_vmcb01, vcpu_svm, vmcb01);
		OFFSET(KVM_VMCB_pa, kvm_vmcb_info, pa);
		OFFSET(SD_save_area_pa, svm_cpu_data, save_area_pa);
	}

	if (IS_ENABLED(CONFIG_KVM_INTEL)) {
		BLANK();
		OFFSET(VMX_spec_ctrl, vcpu_vmx, spec_ctrl);
		OFFSET(VMX_loaded_vmcs, vcpu_vmx, loaded_vmcs);
		OFFSET(VMX_cpuid_fast_cache, vcpu_vmx, cpuid_fast_cache);
		OFFSET(VMX_cpuid_fast_mru, vcpu_vmx, cpuid_fast_mru);
		OFFSET(VMX_stat_exits, vcpu_vmx, vcpu.stat.exits);
		OFFSET(VMX_stat_cpuid_fastpath, vcpu_vmx,
		       vcpu.stat.cpuid_fastpath);
		OFFSET(VMX_stat_cpuid_inner_fastpath, vcpu_vmx,
		       vcpu.stat.cpuid_inner_fastpath);
		OFFSET(VMX_stat_cpuid_inner_cache_miss, vcpu_vmx,
		       vcpu.stat.cpuid_inner_cache_miss);
		OFFSET(VMX_cpuid_profile_tsc_start, vcpu_vmx,
		       cpuid_profile_tsc_start);
		OFFSET(VMX_cpuid_profile_tsc_validation, vcpu_vmx,
		       cpuid_profile_tsc_validation);
		OFFSET(VMX_cpuid_profile_tsc_lookup, vcpu_vmx,
		       cpuid_profile_tsc_lookup);
		OFFSET(VMX_cpuid_profile_tsc_rip, vcpu_vmx,
		       cpuid_profile_tsc_rip);
		OFFSET(VMX_cpuid_profile_tsc_resume, vcpu_vmx,
		       cpuid_profile_tsc_resume);
		OFFSET(VMX_cpuid_profile_divider, vcpu_vmx,
		       cpuid_profile_divider);
		OFFSET(VMX_cpuid_profile_transition_tsc_start, vcpu_vmx,
		       cpuid_profile_transition_tsc_start);
		OFFSET(VMX_cpuid_profile_transition_elapsed, vcpu_vmx,
		       cpuid_profile_transition_elapsed);
		OFFSET(VMX_cpuid_profile_transition_armed, vcpu_vmx,
		       cpuid_profile_transition_armed);
		OFFSET(VMX_cpuid_profile_transition_ready, vcpu_vmx,
		       cpuid_profile_transition_ready);
		OFFSET(VMX_stat_cpuid_profile_samples, vcpu_vmx,
		       vcpu.stat.cpuid_profile_samples);
		OFFSET(VMX_stat_cpuid_profile_validation_cycles, vcpu_vmx,
		       vcpu.stat.cpuid_profile_validation_cycles);
		OFFSET(VMX_stat_cpuid_profile_lookup_cycles, vcpu_vmx,
		       vcpu.stat.cpuid_profile_lookup_cycles);
		OFFSET(VMX_stat_cpuid_profile_rip_cycles, vcpu_vmx,
		       vcpu.stat.cpuid_profile_rip_cycles);
		OFFSET(VMX_stat_cpuid_profile_resume_cycles, vcpu_vmx,
		       vcpu.stat.cpuid_profile_resume_cycles);
		OFFSET(VMX_stat_cpuid_profile_transition_samples, vcpu_vmx,
		       vcpu.stat.cpuid_profile_transition_samples);
		OFFSET(VMX_stat_cpuid_profile_transition_cycles, vcpu_vmx,
		       vcpu.stat.cpuid_profile_transition_cycles);
		OFFSET(VMX_stat_cpuid_profile_transition_min_cycles, vcpu_vmx,
		       vcpu.stat.cpuid_profile_transition_min_cycles);
		OFFSET(VMX_stat_cpuid_profile_transition_max_cycles, vcpu_vmx,
		       vcpu.stat.cpuid_profile_transition_max_cycles);
		OFFSET(LOADED_VMCS_launched, loaded_vmcs, launched);
		OFFSET(VMX_CPUID_CACHE_function, vmx_cpuid_fast_cache_entry,
		       function);
		OFFSET(VMX_CPUID_CACHE_index, vmx_cpuid_fast_cache_entry, index);
		OFFSET(VMX_CPUID_CACHE_eax, vmx_cpuid_fast_cache_entry, eax);
		OFFSET(VMX_CPUID_CACHE_ebx, vmx_cpuid_fast_cache_entry, ebx);
		OFFSET(VMX_CPUID_CACHE_ecx, vmx_cpuid_fast_cache_entry, ecx);
		OFFSET(VMX_CPUID_CACHE_edx, vmx_cpuid_fast_cache_entry, edx);
		OFFSET(VMX_CPUID_CACHE_flags, vmx_cpuid_fast_cache_entry, flags);
		DEFINE(VMX_CPUID_CACHE_entry_size,
		       sizeof(struct vmx_cpuid_fast_cache_entry));
		DEFINE(VMX_CPUID_CACHE_mask, VMX_CPUID_FAST_CACHE_SIZE - 1);
		DEFINE(VMX_CPUID_CACHE_probes, VMX_CPUID_FAST_CACHE_PROBES);
		DEFINE(VMX_CPUID_CACHE_valid, VMX_CPUID_FAST_CACHE_VALID);
		DEFINE(VMX_CPUID_CACHE_indexed, VMX_CPUID_FAST_CACHE_INDEXED);
	}
}
