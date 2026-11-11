#!/bin/bash
# Copyright (c) 2026 Pasqal. Licensed under the Pasqal Open-Source Software License
# (MIT-derived); see LICENSE in the repository root.
# The quantum step of the TmMgGaO4 field sweep: run the sweep `tmgo.py prepare`
# built through QFw (`tmgo.py qfw`) inside a two-group heterogeneous allocation,
# with QFw's shim QPM on the first group and the sweep on the second.
# tmgo-pipeline.sh submits it after the prepare job:
#   sbatch -N1 -n1 : -N1 -n1 --wrap "$PWD/tmgo/tmgo-qfw.sh --device-id pasqal-emu-free \
#       --prepared $PWD/prepared.json --out $PWD/record.json"
# --lib qrmi|qdmi picks the library the QPM reaches the device with (default qrmi).
# --program runs another program's `qfw` command instead of tmgo.py's (the Qiskit
# extra). Needs an activated QFw (openQSE/QFw main) and the Pasqal credentials in
# the environment (demo.sh passes them).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$QFW_SHARE_DIR/examples/qfw_example_common.sh"

device_id=pasqal-emu-free prepared="" out="" lib=qrmi program="${here}/tmgo.py"
while [[ $# -gt 0 ]]; do
	case "$1" in
		--device-id) device_id="$2"; shift 2 ;;
		--prepared) prepared="$2"; shift 2 ;;
		--out) out="$2"; shift 2 ;;
		--lib) lib="$2"; shift 2 ;;
		--program) program="$2"; shift 2 ;;
		*) echo "ERROR: unknown option '$1'" >&2; exit 1 ;;
	esac
done
# The reservation is sized from the prepared sweep: atoms, shots per point, points.
read -r qubits shots points < <(python3 -c 'import json, sys
r = json.load(open(sys.argv[1]))
print(len(r["qubit_ids"]), r["shots"], len(r["sweep"]))' "${prepared}")

runtime_dir="${QFW_RUN_BASE_DIR:-${TMPDIR:-/tmp}/qfw-runs}/example-runtime"
mkdir -p "${runtime_dir}"
device_access="${runtime_dir}/tmgo-device-access.yaml"
services="${runtime_dir}/tmgo-services.yaml"
sed -e "s|QFW_PASQAL_PROJECT_ID|${QFW_PASQAL_PROJECT_ID:-}|" \
	"${here}/qfw-devices.yaml" > "${device_access}"
sed -E "s|^([[:space:]]*device-id:).*|\1 ${device_id}|" \
	"$(qfw_example_path qfw_pulser_services.yaml)" > "${services}"

echo "TmMgGaO4 sweep on ${device_id}: ${qubits} atoms, ${points} points x ${shots} shots"
qfw_example_begin "tmgo"
# Teardown deletes the QPM's log: keep a copy beside the record, and show how the QPM
# reached the device.
export QFW_EXAMPLE_LOG_ARCHIVE_DIR="$(dirname "${out}")/qpm-logs-${SLURM_JOB_ID}"
trap 'rc=$?; trap - EXIT; qfw_example_finish "${rc}"
	grep -rhoE "QDMI backend opened.*|routing [a-z_]+ -> [a-z]+" "${QFW_EXAMPLE_LOG_ARCHIVE_DIR}" 2>/dev/null |
		sort | uniq -c | sed -E "s/^ *([0-9]+) /QPM log, \1x: /"; exit "${rc}"' EXIT
QFW_EXAMPLE_SITE_CONFIG="$(qfw_example_make_site_config "${device_access}")"
QFW_SITE_CONFIG="${QFW_EXAMPLE_SITE_CONFIG}" \
	qfw_example_setup_local_services "${services}" shim-pasqal
DEFW_ONLY_LOAD_MODULE=api_qpm_execution,api_qpm_telemetry,api_qpm_control \
	qfw_example_slurm_driver \
		--backend pasqal \
		--example tmgo \
		--qubits "${qubits}" \
		--shots "${shots}" \
		--count "${points}" \
		--operation async_run \
		--walltime 180000 \
		--ttl 180000 \
		--nodes 1 \
		--ntasks 1 \
		--target-device "${device_id}" \
		-- "${program}" qfw --device-id "${device_id}" --lib "${lib}" --prepared "${prepared}" --out "${out}"
