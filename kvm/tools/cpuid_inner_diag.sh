#!/usr/bin/env bash
set -u

PARAM_DIR=/sys/module/kvm_intel/parameters
DEBUG_ROOT=/sys/kernel/debug/kvm

if (( EUID != 0 )); then
	printf 'Run as root: sudo bash %s [--enable-log|--disable-log]\n' "$0" >&2
	exit 2
fi

case "${1:-}" in
--enable-log)
	printf 'Y\n' > "$PARAM_DIR/fast_cpuid_inner_debug"
	;;
--disable-log)
	printf 'N\n' > "$PARAM_DIR/fast_cpuid_inner_debug"
	;;
"")
	;;
*)
	printf 'Usage: sudo bash %s [--enable-log|--disable-log]\n' "$0" >&2
	exit 2
	;;
esac

decode_blockers()
{
	local mask=$1 bit name found=0
	local -a reasons=(
		'0x1:disabled switch or non-x86_64'
		'0x2:forced immediate exit'
		'0x4:KVM entry/exit/CPUID tracing'
		'0x8:L1D VM-entry flush'
		'0x10:nested guest or SMM'
		'0x20:guest debug or unsynchronized debug registers'
		'0x40:CPUID faulting'
		'0x80:dirty dynamic CPUID state'
		'0x100:PMU counting retired instructions'
		'0x200:Xen CPUID mode'
	)

	if (( mask == 0 )); then
		printf 'eligible'
		return
	fi
	for reason in "${reasons[@]}"; do
		bit=${reason%%:*}
		name=${reason#*:}
		if (( mask & bit )); then
			(( found )) && printf ', '
			printf '%s' "$name"
			found=1
		fi
	done
}

printf 'kernel=%s\n' "$(uname -r)"
printf 'module=%s\n' "$(modinfo -F filename kvm_intel 2>/dev/null || printf unavailable)"
for name in fast_cpuid fast_cpuid_direct fast_cpuid_inner fast_cpuid_inner_debug \
	fast_cpuid_inner_skip_rfds_pcore fast_cpuid_inner_early \
	fast_cpuid_inner_lazy_perf fast_cpuid_inner_relaxed \
	fast_cpuid_inner_profile; do
	if [[ -r "$PARAM_DIR/$name" ]]; then
		printf '%s=' "$name"
		cat "$PARAM_DIR/$name"
	fi
done

printf '\nmitigations:\n'
for name in reg_file_data_sampling l1tf mds tsx_async_abort mmio_stale_data; do
	if [[ -r "/sys/devices/system/cpu/vulnerabilities/$name" ]]; then
		printf '  %s: ' "$name"
		cat "/sys/devices/system/cpu/vulnerabilities/$name"
	fi
done

printf '\nper-vCPU inner-fastpath state and counters:\n'
mapfile -t files < <(find "$DEBUG_ROOT" -type f \
	\( -path '*/vcpu*/cpuid_inner_*' -o -path '*/vcpu*/cpuid_profile_*' \) \
	2>/dev/null | sort)
if (( ${#files[@]} == 0 )); then
	printf '  unavailable (keep the VM running and ensure debugfs is mounted)\n'
else
	for file in "${files[@]}"; do
		value=$(<"$file")
		printf '  %s=%s' "$file" "$value"
		if [[ ${file##*/} == cpuid_inner_blockers ]]; then
			printf ' ('
			decode_blockers "$value"
			printf ')'
		fi
		printf '\n'
	done
fi

printf '\nsampled CPUID profile averages (cycles per sampled direct hit):\n'
mapfile -t profile_samples < <(find "$DEBUG_ROOT" -type f \
	-path '*/vcpu*/cpuid_profile_samples' 2>/dev/null | sort)
if (( ${#profile_samples[@]} == 0 )); then
	printf '  unavailable (arm fast_cpuid_inner_profile and run the benchmark first)\n'
else
	for file in "${profile_samples[@]}"; do
		dir=${file%/cpuid_profile_samples}
		samples=$(<"$file")
		if (( samples == 0 )); then
			printf '  %s samples=0\n' "$dir"
			continue
		fi
		validation=$(<"$dir/cpuid_profile_validation_cycles")
		lookup=$(<"$dir/cpuid_profile_lookup_cycles")
		rip=$(<"$dir/cpuid_profile_rip_cycles")
		resume=$(<"$dir/cpuid_profile_resume_cycles")
		awk -v d="$dir" -v n="$samples" -v v="$validation" \
			-v l="$lookup" -v r="$rip" -v p="$resume" \
			'BEGIN {
			 a=v/n; b=(l-v)/n; c=(r-l)/n; e=(p-r)/n;
			 max=a; name="validation";
			 if (b > max) { max=b; name="lookup" }
			 if (c > max) { max=c; name="rip/vmwrite" }
			 if (e > max) { max=e; name="pre_resume" }
			 printf "  %s samples=%d validation=%.2f lookup=%.2f rip/vmwrite=%.2f pre_resume=%.2f dominant=%s(%.2f)\n", \
			 d, n, a, b, c, e, name, max
			}'
		if [[ -r "$dir/cpuid_profile_transition_samples" &&
		      -r "$dir/cpuid_profile_transition_cycles" ]]; then
			transition_samples=$(<"$dir/cpuid_profile_transition_samples")
			transition_cycles=$(<"$dir/cpuid_profile_transition_cycles")
			if (( transition_samples > 0 )); then
				transition_min=$(<"$dir/cpuid_profile_transition_min_cycles")
				transition_max=$(<"$dir/cpuid_profile_transition_max_cycles")
				awk -v d="$dir" -v n="$transition_samples" \
					-v c="$transition_cycles" -v lo="$transition_min" \
					-v hi="$transition_max" \
					'BEGIN {
					 printf "  %s transition_samples=%d vmresume_to_cpuid_vmexit_min/avg/max=%.0f/%.2f/%.0f\n", d, n, lo, c/n, hi
					}'
			fi
		fi
	done
fi

printf '\nVMCS transition snapshots (controls are hexadecimal):\n'
mapfile -t vmcs_valid < <(find "$DEBUG_ROOT" -type f \
	-path '*/vcpu*/cpuid_profile_vmcs_valid' 2>/dev/null | sort)
if (( ${#vmcs_valid[@]} == 0 )); then
	printf '  unavailable\n'
else
	for file in "${vmcs_valid[@]}"; do
		dir=${file%/cpuid_profile_vmcs_valid}
		valid=$(<"$file")
		if (( valid == 0 )); then
			printf '  %s valid=0\n' "$dir"
			continue
		fi
		exit_store=$(<"$dir/cpuid_profile_vm_exit_msr_store_count")
		exit_load=$(<"$dir/cpuid_profile_vm_exit_msr_load_count")
		entry_load=$(<"$dir/cpuid_profile_vm_entry_msr_load_count")
		exit_ctl=$(<"$dir/cpuid_profile_vm_exit_controls")
		entry_ctl=$(<"$dir/cpuid_profile_vm_entry_controls")
		primary_ctl=$(<"$dir/cpuid_profile_primary_exec_controls")
		secondary_ctl=$(<"$dir/cpuid_profile_secondary_exec_controls")
		printf '  %s msr_store=%s msr_exit_load=%s msr_entry_load=%s exit=0x%x entry=0x%x primary=0x%x secondary=0x%x\n' \
			"$dir" "$exit_store" "$exit_load" "$entry_load" \
			"$exit_ctl" "$entry_ctl" "$primary_ctl" "$secondary_ctl"
		if [[ -r "$dir/tsc-scaling-ratio" &&
		      -r "$dir/tsc-scaling-ratio-frac-bits" ]]; then
			tsc_ratio=$(<"$dir/tsc-scaling-ratio")
			tsc_frac_bits=$(<"$dir/tsc-scaling-ratio-frac-bits")
			printf '    tsc_scaling_ratio=%s fractional_bits=%s\n' \
				"$tsc_ratio" "$tsc_frac_bits"
		fi
	done
fi

printf '\nsampled VM-entry/exit state-load audit:\n'
printf '  masks: perf=0x1 pat=0x2 efer=0x4 cet=0x8\n'
mapfile -t state_samples_files < <(find "$DEBUG_ROOT" -type f \
	-path '*/vcpu*/cpuid_profile_state_samples' 2>/dev/null | sort)
if (( ${#state_samples_files[@]} == 0 )); then
	printf '  unavailable (requires timing14 or newer)\n'
else
	for file in "${state_samples_files[@]}"; do
		dir=${file%/cpuid_profile_state_samples}
		state_samples=$(<"$file")
		if (( state_samples == 0 )); then
			printf '  %s state_samples=0\n' "$dir"
			continue
		fi
		active_mask=$(<"$dir/cpuid_profile_state_active_mask")
		equal_mask=$(<"$dir/cpuid_profile_state_equal_mask")
		printf '  %s state_samples=%s latest_active=0x%x latest_equal=0x%x\n' \
			"$dir" "$state_samples" "$active_mask" "$equal_mask"
		for state in perf pat efer cet; do
			active=$(<"$dir/cpuid_profile_state_${state}_active_samples")
			equal=$(<"$dir/cpuid_profile_state_${state}_equal_samples")
			printf '    %s active_samples=%s equal_samples=%s\n' \
				"$state" "$active" "$equal"
		done
		guest=$(<"$dir/cpuid_profile_state_guest_perf_global_ctrl")
		host=$(<"$dir/cpuid_profile_state_host_perf_global_ctrl")
		printf '    PERF_GLOBAL_CTRL guest=0x%016x host=0x%016x\n' \
			"$guest" "$host"
		guest=$(<"$dir/cpuid_profile_state_guest_pat")
		host=$(<"$dir/cpuid_profile_state_host_pat")
		printf '    PAT guest=0x%016x host=0x%016x\n' "$guest" "$host"
		guest=$(<"$dir/cpuid_profile_state_guest_efer")
		host=$(<"$dir/cpuid_profile_state_host_efer")
		printf '    EFER guest=0x%016x host=0x%016x\n' "$guest" "$host"
		guest_s_cet=$(<"$dir/cpuid_profile_state_guest_s_cet")
		host_s_cet=$(<"$dir/cpuid_profile_state_host_s_cet")
		guest_ssp=$(<"$dir/cpuid_profile_state_guest_ssp")
		host_ssp=$(<"$dir/cpuid_profile_state_host_ssp")
		guest_intr_ssp=$(<"$dir/cpuid_profile_state_guest_intr_ssp_table")
		host_intr_ssp=$(<"$dir/cpuid_profile_state_host_intr_ssp_table")
		printf '    CET s_cet=0x%016x/0x%016x ssp=0x%016x/0x%016x intr_ssp=0x%016x/0x%016x (guest/host)\n' \
			"$guest_s_cet" "$host_s_cet" "$guest_ssp" "$host_ssp" \
			"$guest_intr_ssp" "$host_intr_ssp"
	done
fi

printf '\nrecent transition logs:\n'
journalctl -k -n 200 --no-pager 2>/dev/null | grep 'CPUID inner' | tail -20 || true
