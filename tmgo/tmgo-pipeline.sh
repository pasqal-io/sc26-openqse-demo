#!/bin/bash
# Copyright (c) 2026 Pasqal. Licensed under the Pasqal Open-Source Software License
# (MIT-derived); see LICENSE in the repository root.
# Run the TmMgGaO4 field sweep as dependent Slurm jobs, so that only the
# quantum steps hold a QFw allocation:
#   prepare   1 task: build the register and the sweep on the device's public specs
#   quantum   QFw two-group job (tmgo-qfw.sh): run the batch through the shim QPM
#   refine    with --refine K, 1 task: find the S(q1/3) peak of that run and
#             prepare K more points around it, then a second quantum job runs them
#   analyze   1 task: compute the observables and plot them
# Each job starts only if the one before it succeeded; a failure cancels the rest.
# Waits for the chain, then prints its accounting and the observables.
#
# Usage: tmgo-pipeline.sh --out-dir DIR [--device-id ID] [--lib qrmi|qdmi] [--refine K] [prepare options...]
#   --device-id  pasqal-emu-free (default), or a QPU: pasqal-fresnel-can1 or pasqal-fresnel
#   --lib        library the QPM reaches the device with: QRMI (default) or QDMI
# Other options go to tmgo.py prepare, e.g. --points 8 --shots 100.
# Needs an activated QFw and the Pasqal credentials in the environment; only the
# quantum jobs inherit the credentials. DIR must be on a filesystem all nodes share.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
device_id=pasqal-emu-free out_dir="" refine=0 lib=qrmi args=()
while [[ $# -gt 0 ]]; do
	case "$1" in
		--device-id) device_id="$2"; shift 2 ;;
		--out-dir) out_dir="$2"; shift 2 ;;
		--refine) refine="$2"; shift 2 ;;
		--lib) lib="$2"; shift 2 ;;
		*) args+=("$1"); shift ;;
	esac
done
mkdir -p "${out_dir}"
out_dir="$(cd "${out_dir}" && pwd)"
prepared="${out_dir}/prepared.json" record="${out_dir}/record.json"

echo "Slurm nodes (sinfo):"
sinfo -o "%.10P %.6D %.8T %N" | sed 's/^/  /'
echo "Submitting the sweep as Slurm jobs, each one after the one before succeeded"
echo "(--dependency=afterok): only the quantum job holds the QFw allocation."
submit() { sbatch --parsable --kill-on-invalid-dep=yes -o "${out_dir}/%x-%j.out" "$@"; }
# Slurm stores a job's environment with it: only the quantum jobs get the Pasqal login.
classical() { unset PASQAL_USERNAME PASQAL_PASSWORD PASQAL_EMAIL; submit "$@"; }  # in $(...): a subshell
# A QPU's queue can take up to the 48 hours QFw waits: ask Slurm for that much, not the partition's
# default. A partition whose limit is lower keeps the job pending (PartitionTimeLimit) before any shot.
limit=(); [[ "${device_id}" == pasqal-emu-free ]] || limit=(-t 2-02:00:00)
prepare=$(classical -J tmgo-prepare -N1 -n1 \
	--wrap "${here}/tmgo.py prepare --device-id ${device_id} --out ${prepared} ${args[*]}")
quantum=$(submit "${limit[@]}" -J tmgo-quantum -d "afterok:${prepare}" -N1 -n1 : -N1 -n1 \
	--wrap "${here}/tmgo-qfw.sh --device-id ${device_id} --lib ${lib} --prepared ${prepared} --out ${record}")
echo "[slurm] job ${prepare} prepare: 1 task, the sweep for ${device_id}"
echo "[slurm] job ${quantum} quantum: after ${prepare}, QFw allocation on ${device_id} through ${lib}"
jobs="${prepare},${quantum}" last="${quantum}" records="${record}"
if (( refine > 0 )); then
	refined="${out_dir}/refined.json" record2="${out_dir}/record-refined.json"
	refine_job=$(classical -J tmgo-refine -d "afterok:${quantum}" -N1 -n1 \
		--wrap "${here}/tmgo.py refine --record ${record} --prepared ${prepared} --points ${refine} --out ${refined}")
	last=$(submit "${limit[@]}" -J tmgo-quantum2 -d "afterok:${refine_job}" -N1 -n1 : -N1 -n1 \
		--wrap "${here}/tmgo-qfw.sh --device-id ${device_id} --lib ${lib} --prepared ${refined} --out ${record2}")
	echo "[slurm] job ${refine_job} refine: after ${quantum}, 1 task, ${refine} points around the S(q1/3) peak"
	echo "[slurm] job ${last} quantum2: after ${refine_job}, QFw allocation on ${device_id} through ${lib}"
	jobs+=",${refine_job},${last}" records+=" ${record2}"
fi
analyze=$(classical -J tmgo-analyze -d "afterok:${last}" -N1 -n1 \
	--wrap "${here}/tmgo.py analyze ${records} --plot ${out_dir}/observables.png")
echo "[slurm] job ${analyze} analyze: after ${last}, 1 task"
jobs+=",${analyze}"

# What a job does, and for a quantum job how far it is, from its output.
describe() {  # name state id
	local f="${out_dir}/${1}-${3}.out"
	case "${1#tmgo-}" in
		prepare) echo "build the register and the sweep for ${device_id}" ;;
		refine) echo "add ${refine} sweep points around the S(q1/3) peak" ;;
		analyze) echo "compute M^z and S(q1/3) per sweep point, plot them" ;;
		quantum*)
			if grep -qE ': [A-Z_/]+ in [0-9.]+s$' "${f}" 2>/dev/null; then
				echo "batch$(grep -oE ': [A-Z_/]+ in [0-9.]+s$' "${f}" | tr -d :), QFw releases the reservation"
			elif grep -q '"event": "reserved"' "${f}" 2>/dev/null; then
				echo "QPM $(grep -om1 'shim-pasqal-[0-9a-f]\{8\}' "${f}") reserved ${device_id}; the batch runs there via ${lib^^}"
			elif [[ "$2" == RUNNING ]]; then
				echo "starting QFw's shim QPM service, reserving ${device_id}"
			else
				echo "QFw allocation: the shim QPM on one node, the sweep on the other"
			fi ;;
	esac
}
rows() {  # one sacct and one squeue call for the whole chain
	local acct reasons id name state nodes
	acct=$(sacct -X -n -P -j "${jobs}" -o JobID,JobName,State,NodeList)
	reasons=$(squeue -h -j "${jobs}" -o '%i|%r' 2>/dev/null)
	for id in ${jobs//,/ }; do
		IFS='|' read -r _ name state _ <<< "$(grep -m1 "^${id}[|+]" <<< "${acct}")"
		nodes=$(grep "^${id}[|+]" <<< "${acct}" | cut -d'|' -f4 | sort -u | paste -sd,)
		[[ "${state}" != PENDING ]] || nodes="($(grep -m1 "^${id}[|+]" <<< "${reasons}" | cut -d'|' -f2))"
		printf '  %-5s %-9s %-10s %-13s %s\n' "${id}" "${name#tmgo-}" "${state}" "${nodes}" "$(describe "${name}" "${state}" "${id}")"
	done
}
frame() {  # rows
	local t=$((SECONDS - start))
	printf 'Job chain after %dm%02ds:\n  %-5s %-9s %-10s %-13s %s\n%s\n' $((t / 60)) $((t % 60)) \
		JOBID STEP STATE NODES "WHAT IT DOES" "$1"
	echo "Your jobs in the queue (squeue -u ${me}; all users: squeue):"
	squeue -u "${me}" -o "%.8i %.14j %.10T %.8M %.20R" | sed 's/^/  /'
	echo "Latest job output (${out_dir}):"
	for id in ${jobs//,/ }; do cat "${out_dir}"/*-"${id}".out 2>/dev/null || :; done |
		awk '/^[A-Za-z]/ && !/^QFW_/' | tail -n 3 | sed 's/^/  | /'
	echo "Check it yourself: squeue; sacct -j ${jobs}; tail -f ${out_dir}/tmgo-quantum-${quantum}.out"
}
# On a terminal the panel is redrawn in place every 2 s, between hide/show-cursor
# codes that demo.sh uses to keep it out of the log; otherwise it is printed when
# a job changes state. Either way the final panel ends up as plain text. A failed
# Slurm query only spoils one panel: it must not end the script while the jobs run,
# so the wait ends only when accounting shows the analyze job in a final state.
me=$(id -un) live=0 cols=1000
[[ -t 1 ]] && live=1 cols=${COLUMNS:-$(tput cols)}
start=${SECONDS} drawn=0 prev=""
until [[ "$(sacct -X -n -P -j "${analyze}" -o State 2>/dev/null)" =~ ^(COMPLETED|FAILED|CANCELLED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL|PREEMPTED|BOOT_FAIL|DEADLINE) ]]; do
	now=$(rows) || :
	if (( live )); then
		f=$(frame "${now}" | expand | fold -s -w "${cols}")
		(( drawn == 0 )) || printf '\033[%dA' "${drawn}"
		printf '\033[?25l%s\033[K\033[J\033[?25h\n' "${f//$'\n'/$'\033[K\n'}"
		drawn=$(wc -l <<< "${f}")
	elif [[ "${now}" != "${prev}" ]]; then
		frame "${now}" || :
	fi
	prev="${now}"
	sleep 2
done
(( drawn == 0 )) || printf '\033[%dA\033[J' "${drawn}"
frame "$(rows)" | expand | fold -s -w "${cols}" || :
echo "Accounting (sacct):"
sacct -X -j "${jobs}" -o JobID,JobName%14,State,Elapsed,AllocCPUS,NodeList
if ! scontrol show job "${analyze}" | grep -q 'JobState=COMPLETED'; then
	echo "the chain did not complete; job output in ${out_dir}" >&2
	exit 1
fi
cat "${out_dir}/tmgo-analyze-${analyze}.out"
