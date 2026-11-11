#!/usr/bin/env python3
# Copyright (c) 2026 Pasqal. Licensed under the Pasqal Open-Source Software License
# (MIT-derived); see LICENSE in the repository root.
"""The TmMgGaO4 field sweep written in Qiskit, run through the same QFw job as the Pulser demo.

Each sweep point is one Pasqal HamiltonianGate, compiled to a Pulser sequence by
qiskit-pasqal-provider and run as its own batch. Everything else is tmgo.py: the register,
the sweep, the QFw job and the analysis. Needs qiskit-pasqal-provider==0.5.0 in QFw's venv.

Usage, in `./demo.sh shell` (the repository root, QFw activated); on a cluster whose image has
QFw, its venv is root's: there, run the pip install as `docker exec slurmctld /opt/openqse/qfw-venv/bin/pip ...`.
    pip install qiskit-pasqal-provider==0.5.0
    extras/qiskit/tmgo_qiskit.py prepare --points 8 --shots 100 --out ../demo-results/qiskit/prepared.json
    sbatch --wait -N1 -n1 : -N1 -n1 --wrap "$PWD/tmgo/tmgo-qfw.sh --program $PWD/extras/qiskit/tmgo_qiskit.py \\
        --device-id pasqal-emu-free --prepared $PWD/../demo-results/qiskit/prepared.json --out $PWD/../demo-results/qiskit/record.json"
    tmgo/tmgo.py analyze ../demo-results/qiskit/record.json --plot ../demo-results/qiskit/observables.png
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tmgo"))
import tmgo  # noqa: E402

POINTS = 21  # points per ramp of the Qiskit sweep


def qiskit_sequence(device, register, delta_f):
	"""Write the sweep point delta_f as one Pasqal HamiltonianGate in a Qiskit circuit; compile it to Pulser.

	The amplitude and detuning follow the Pulser sweep through POINTS points per ramp: Pulser
	serializes only PCHIP interpolation, which is linear through collinear points. The point is bound
	before compiling: qiskit-pasqal-provider 0.5.0 cannot serialize a parametrized HamiltonianGate.
	"""
	from qiskit import QuantumCircuit
	from qiskit_pasqal_provider.providers.gate import HamiltonianGate
	from qiskit_pasqal_provider.providers.pulse_utils import InterpolatePoints, gen_seq
	omega, delta0, sweep = tmgo.sweep_shape(device)
	corners, total = [0, tmgo.RISE, tmgo.RISE + sweep], tmgo.RISE + sweep
	times = np.unique(np.concatenate([np.linspace(a, b, POINTS) for a, b in zip(corners, corners[1:])]))
	waveform = lambda values: InterpolatePoints(values=list(np.interp(times, corners, values)),
		times=list(times / total), duration=total)
	ids = list(register.qubit_ids)
	gate = HamiltonianGate(waveform([0.0, omega, omega]), waveform([delta0, delta0, delta_f]), 0.0,
		coords=[register.qubits[q].as_array() for q in ids])
	circuit = QuantumCircuit(len(ids))
	circuit.append(gate, range(len(ids)))
	return gen_seq(register, device, circuit)


def prepare_run(args):
	"""Build the sweep, check that every point compiles, and save it for `qfw` with the device and register."""
	device = tmgo.load_device(args.device_id)
	record, seq, points = tmgo.prepare(device, args, None)
	register = seq.register
	for delta_f in points:
		qiskit_sequence(device, register, delta_f)
	tmgo.save(record | {"pass": "coarse sweep", "sweep": points, "written": ", written in Qiskit",
		"device_specs": device.to_abstract_repr(), "register": register.to_abstract_repr()}, args.out)
	print(f"Prepared {len(points)} points x {args.shots} shots in Qiskit on {record['device']}: {args.out}")


def qiskit_batches(record, points, connection):
	"""Run each point as its own batch, one after another (QRMI refuses a submission while one is in flight)."""
	import pulser
	from pulser.abstract_repr import deserialize_device
	from pulser.register import Register
	device = deserialize_device(record.pop("device_specs"))
	register = Register.from_abstract_repr(record.pop("register"))
	batches = [pulser.QPUBackend(qiskit_sequence(device, register, v), connection=connection)
		.run([{"runs": record["shots"]}], wait=True) for v in points]
	results = [next(iter(b.get_available_results().values()), None) for b in batches]
	return results, {b.get_batch_status().name for b in batches}


if __name__ == "__main__":
	parser = tmgo.parser()
	args = parser.parse_args()
	if args.command not in ("prepare", "qfw"):
		parser.error("tmgo_qiskit.py runs prepare and qfw; use tmgo.py for the rest")
	prepare_run(args) if args.command == "prepare" else tmgo.run_qfw(args, submit=qiskit_batches)
