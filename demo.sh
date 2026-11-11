#!/bin/bash
# Copyright (c) 2026 Pasqal. Licensed under the Pasqal Open-Source Software License
# (MIT-derived); see LICENSE in the repository root.
# Run the TmMgGaO4 field sweep on a running QFw-SLURM-Cluster (see README.md): three
# Slurm jobs, with QFw reserving the Pasqal device for the quantum one only.
#
# Usage: demo.sh setup          once: build QFw with the Pasqal packages in the cluster
#        demo.sh [options...]   run the sweep; options go to tmgo/tmgo-pipeline.sh, e.g.
#                               --device-id pasqal-fresnel-can1 for the FRESNEL_CAN1 QPU,
#                               --lib qdmi, --refine 4, --points 12, --shots 500
#        demo.sh shell          a shell in the cluster, QFw activated, for extras/
# Needs ~/.pasqal/config with username, password and project_id lines.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B=/workspace/qfw-container-base
QFW_COMMIT=bc080b5  # openQSE/QFw main with the Pulser frontend (#158) and Pasqal over QDMI (#182)
# The cluster's controller is the slurmctld container with the QFw workspace mounted (other
# slurmctld containers print an empty line).
found=$(for c in $(docker ps --format '{{.Names}}' -f label=com.docker.compose.service=slurmctld); do
	docker inspect -f "{{range .Mounts}}{{if eq .Destination \"${B}\"}}${c} {{.Source}}{{end}}{{end}}" "${c}"; done | awk NF)
[[ -n "${found}" ]] || { echo "no running QFw-SLURM-Cluster: start one as README.md says" >&2; exit 1; }
read -r ctld shared <<< "${found}"
mkdir -p "${here}/logs" "${here}/results"
# Jobs run as a regular user, not root: on some clusters root's batch jobs have killed
# slurmd. That is the first user with a Slurm account, whose account the jobs then name,
# or else the first regular user. Newer cluster images ship QFw in /opt/openqse: then it
# is used and setup is not needed.
until docker exec "${ctld}" sacctmgr --immediate list cluster >/dev/null 2>&1; do sleep 2; done # slurmdbd up
read -r user account < <(docker exec "${ctld}" sacctmgr -n -P list assoc format=User,Account |
	awk -F'|' '$1 != "" && $1 != "root" { print $1, $2; exit }') || :
[[ -n "${user}" ]] || user=$(docker exec "${ctld}" getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 { print $1; exit }')
user="${user:-root}"
image_qfw=/opt/openqse/qfw/bin/qfw-activate

if [[ "${1:-}" == setup ]]; then
	# Leave a cluster that already has QFw alone.
	docker exec "${ctld}" test -e "${image_qfw}" &&
		{ echo "this cluster's image has QFw in /opt/openqse: run ${here}/demo.sh, no setup needed" >&2; exit 1; }
	[[ -z "$(git -C "${shared}/QFw" status --porcelain 2>/dev/null)" ]] ||
		{ echo "${shared}/QFw has local changes: setup would check out ${QFW_COMMIT} over them" >&2; exit 1; }
	# The cluster image predates the qfw-slurm job_submit and burst-buffer plugins its
	# slurm.conf names: drop them, register the cluster, restart it and wait for c1 and c2.
	docker exec "${ctld}" bash -c "sed -i -e '/^JobSubmitPlugins=lua/d' -e '/^BurstBufferType=burst_buffer\/lua/d' \
		/etc/slurm/slurm.conf && touch /etc/slurm/plugstack.conf
		sacctmgr -n -P list cluster name=linux format=Cluster | grep -qx linux || sacctmgr -i add cluster name=linux"
	project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "${ctld}")
	docker restart $(docker ps -q -f label=com.docker.compose.project="${project}") >/dev/null
	until [[ "$(docker exec "${ctld}" sinfo -h -p normal -n c1,c2 -t idle -o %D 2>/dev/null)" == 2 ]]; do sleep 3; done
	[[ -d "${shared}/QFw/.git" ]] || git clone -q https://github.com/openQSE/QFw.git "${shared}/QFw"
	git -C "${shared}/QFw" fetch -q origin
	git -C "${shared}/QFw" checkout -q "${QFW_COMMIT}"
	git -C "${shared}/QFw" submodule update -q --init --recursive
	echo "building QFw ${QFW_COMMIT} in the cluster (a few minutes, log in logs/setup.log)"
	# QFw's own build steps, without the MQT compiler (it needs a newer cluster image), plus
	# Pulser, QRMI and, for --lib qdmi, the pasqal-qdmi device at the versions QFw pins.
	docker exec "${ctld}" bash -lc "set -e; cd ${B}; export PKG_CONFIG_PATH=/opt/qfw/libfabric/lib/pkgconfig
		rm -rf qfw-build qfw-install; [ -d qfw-venv ] || python3 -m venv qfw-venv; source qfw-venv/bin/activate
		pip install -q --upgrade pip setuptools wheel
		pip install -q -r QFw/setup/build-requirements.txt -r QFw/setup/requirements.txt \
			-r QFw/setup/pulser-requirements.txt 'qrmi==0.26.1' 'jsonschema>=4' \
			\$(grep -E '^(mqt-core|pasqal-qdmi)' QFw/setup/qdmi-requirements.txt)
		cmake -S QFw -B qfw-build -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_INSTALL_PREFIX=${B}/qfw-install \
			-DQFW_BUILD_BUNDLED_DEFW=ON
		cmake --build qfw-build -j 4 && cmake --install qfw-build
		chown -R ${user} qfw-venv  # so that the extras can pip install into it" > "${here}/logs/setup.log" 2>&1 ||
		{ tail -30 "${here}/logs/setup.log"; exit 1; }
	echo "QFw is ready: run ${here}/demo.sh"
	exit
fi

# The Pasqal credentials reach the jobs through the environment only, passed by name.
cfg() {  # a key's value, without the quotes around it
	sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" ~/.pasqal/config | tr -d '\r' |
		sed -E "s/[[:space:]]+\$//; s/^(['\"])(.*)\1\$/\2/"
}
export PASQAL_USERNAME="$(cfg username)" PASQAL_PASSWORD="$(cfg password)" QFW_PASQAL_PROJECT_ID="$(cfg project_id)"
[[ -n "${PASQAL_USERNAME}" && -n "${PASQAL_PASSWORD}" && -n "${QFW_PASQAL_PROJECT_ID}" ]] ||
	{ echo "~/.pasqal/config needs username, password and project_id lines" >&2; exit 1; }
export PASQAL_EMAIL="${PASQAL_USERNAME}"  # the name pasqal-qdmi reads
env=(-e PASQAL_USERNAME -e PASQAL_EMAIL -e PASQAL_PASSWORD -e QFW_PASQAL_PROJECT_ID ${account:+-e SBATCH_ACCOUNT=${account}})
# The cluster runs this checkout's copy, on the filesystem all its nodes share.
rsync -a --delete --exclude .git --exclude __pycache__ --exclude cluster --exclude logs --exclude results \
	"${here}/" "${shared}/demo/"
# QFw's run files go where all nodes see them, not in its default /tmp/qfw-runs.
activate="if [ -e ${image_qfw} ]; then source ${image_qfw} --venv /opt/openqse/qfw-venv
	else source ${B}/qfw-install/bin/qfw-activate --venv ${B}/qfw-venv; fi >/dev/null
	export QFW_SHARED_ROOT=${B} QFW_RUN_BASE_DIR=${B}/qfw-runs"
docker exec "${ctld}" install -d -o "${user}" "${B}/demo-results" "${B}/qfw-runs"

if [[ "${1:-}" == shell ]]; then
	exec docker exec -it -u "${user}" -w "${B}/demo" "${env[@]}" "${ctld}" bash -c "${activate}; exec bash -i"
fi

stamp="$(date +%Y%m%d-%H%M%S)"
log="${here}/logs/${stamp}.log"
echo "TmMgGaO4 field sweep $*; watch the queue from another terminal: docker exec ${ctld} squeue"
# On a terminal the pipeline redraws its status panel in place; the log keeps
# everything but the redrawn panels, without terminal codes.
tty=()
[[ -t 1 ]] && tty=(-t -e COLUMNS="$(tput cols)")
rc=0  # a failed chain still gets its log and results
docker exec "${tty[@]}" -u "${user}" -w "${B}" "${env[@]}" "${ctld}" bash -c "${activate}
	export QFW_RUN_BASE_DIR=${B}/qfw-runs/${stamp}
	demo/tmgo/tmgo-pipeline.sh --out-dir demo-results/${stamp} \"\$@\"" _ "$@" 2>&1 | tee "${log}.raw" || rc=$?
tr -d '\r' < "${log}.raw" | awk '/\033\[\?25l/, /\033\[\?25h/ { next }
	{ gsub(/\033\[[0-9;?]*[A-Za-z]/, ""); print }' > "${log}" && rm "${log}.raw"
[[ -d "${shared}/demo-results/${stamp}" ]] && cp -r "${shared}/demo-results/${stamp}" "${here}/results/"
plot="${here}/results/${stamp}/observables.png"
if [[ -f "${plot}" ]]; then  # shown on a desktop: macOS, or Linux with a display
	if [[ "$(uname)" == Darwin ]]; then open "${plot}"
	elif [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then xdg-open "${plot}" >/dev/null 2>&1 &
	else echo "plot: results/${stamp}/observables.png"; fi
fi
echo "log: logs/${stamp}.log"
exit "${rc}"
