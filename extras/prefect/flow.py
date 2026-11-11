#!/usr/bin/env python3
# Copyright (c) 2026 Pasqal. Licensed under the Pasqal Open-Source Software License
# (MIT-derived); see LICENSE in the repository root.
"""The TmMgGaO4 field sweep as a Prefect flow: the same steps as demo.sh's Slurm chain.

Each task submits one Slurm job and waits for it, so the flow holds no QFw state: the quantum
steps run in QFw's two-group allocation (tmgo-qfw.sh), the others are one-task jobs of tmgo.py.
Prefect lives in a venv of its own; the jobs keep the shell's QFw environment.

Usage, in `./demo.sh shell`:
    python3 -m venv ~/prefect-venv && ~/prefect-venv/bin/pip install -r extras/prefect/requirements.txt
    ~/prefect-venv/bin/python extras/prefect/flow.py --out-dir $PWD/../demo-results/prefect [--device-id ID] [--lib qdmi]
"""

import argparse
import os
import subprocess
from pathlib import Path

# Prefect's temporary server keeps its database off the shared filesystem.
os.environ.setdefault("PREFECT_HOME", "/tmp/prefect")
os.environ.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "false")
from prefect import flow, task  # noqa: E402

TMGO = Path(__file__).resolve().parents[2] / "tmgo"


def sbatch(name, out_dir, command, groups=1, limit=()):
	"""Submit `command` as a Slurm job of one task per group and wait for it."""
	layout = ["-N1", "-n1"] + [":", "-N1", "-n1"] * (groups - 1)
	# Slurm stores a job's environment with it: only the quantum jobs get the Pasqal login.
	login = ("PASQAL_USERNAME", "PASQAL_PASSWORD", "PASQAL_EMAIL")
	env = {k: v for k, v in os.environ.items() if groups > 1 or k not in login}
	job = subprocess.run(["sbatch", "--wait", "--parsable", "-J", name, "-o", f"{out_dir}/%x-%j.out", *limit, *layout,
		"--wrap", command], capture_output=True, text=True, env=env)
	if job.returncode:
		raise RuntimeError(f"Slurm job {name} ({job.stdout.strip()}) failed: {job.stderr.strip()}")
	print(f"[slurm] job {job.stdout.strip()} {name}: COMPLETED", flush=True)


@task
def prepare(out_dir, device_id, size, points, shots):
	"""Build the register and the sweep on the device's public specs."""
	out = f"{out_dir}/prepared.json"
	sbatch("tmgo-prepare", out_dir, f"{TMGO}/tmgo.py prepare --device-id {device_id} "
		+ (f"--size {size} " if size else "") + f"--points {points} --shots {shots} --out {out}")
	return out


@task
def quantum(out_dir, prepared, out, device_id, lib):
	"""Run a prepared sweep in a QFw allocation: the QPM on group 1, the sweep on group 2."""
	# A QPU's queue can take up to the 48 hours QFw waits, as in tmgo-pipeline.sh.
	limit = () if device_id == "pasqal-emu-free" else ("-t", "2-02:00:00")
	sbatch("tmgo-quantum", out_dir, f"{TMGO}/tmgo-qfw.sh --device-id {device_id} --lib {lib} "
		f"--prepared {prepared} --out {out}", groups=2, limit=limit)
	return out


@task
def refine(out_dir, record, prepared, points):
	"""Prepare more points around the S(q1/3) peak of a finished run."""
	out = f"{out_dir}/refined.json"
	sbatch("tmgo-refine", out_dir, f"{TMGO}/tmgo.py refine --record {record} --prepared {prepared} "
		f"--points {points} --out {out}")
	return out


@task
def analyze(out_dir, records):
	"""Compute the observables of the records and plot them."""
	sbatch("tmgo-analyze", out_dir, f"{TMGO}/tmgo.py analyze {' '.join(records)} --plot {out_dir}/observables.png")


@flow(name="tmgo-field-sweep", log_prints=True)
def field_sweep(out_dir, device_id="pasqal-emu-free", lib="qrmi", refine_points=0, size=None, points=8, shots=100):
	"""Run the sweep, refine it around the S(q1/3) peak and analyse both runs."""
	prepared = prepare(out_dir, device_id, size, points, shots)
	records = [quantum(out_dir, prepared, f"{out_dir}/record.json", device_id, lib)]
	if refine_points:
		refined = refine(out_dir, records[0], prepared, refine_points)
		records.append(quantum(out_dir, refined, f"{out_dir}/record-refined.json", device_id, lib))
	analyze(out_dir, records)


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	parser.add_argument("--out-dir", required=True, help="on a filesystem all nodes share")
	parser.add_argument("--device-id", default="pasqal-emu-free", choices=("pasqal-emu-free", "pasqal-fresnel-can1", "pasqal-fresnel"))
	parser.add_argument("--lib", default="qrmi", choices=("qrmi", "qdmi"))
	parser.add_argument("--refine", type=int, default=0, dest="refine_points", help="more points around the peak")
	parser.add_argument("--size", type=int, help="rhombus side: 3 on the emulator, 7 on a QPU")
	for opt, default in (("points", 8), ("shots", 100)):
		parser.add_argument(f"--{opt}", type=int, default=default)
	args = parser.parse_args()
	os.makedirs(args.out_dir, exist_ok=True)
	field_sweep(**vars(args))
