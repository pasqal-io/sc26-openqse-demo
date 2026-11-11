#!/usr/bin/env python3
# Copyright (c) 2026 Pasqal. Licensed under the Pasqal Open-Source Software License
# (MIT-derived); see LICENSE in the repository root.
"""Field sweep of the triangular-lattice Ising model of TmMgGaO4 on a Pasqal device.

Follows the quasi-adiabatic protocol of Leclerc et al., "One-to-one quantum
simulation of a frustrated magnet with 256 qubits", arXiv:2603.20372 (SM Note 2
and 3), on an L x L rhombus of a device's calibrated triangular layout. The
Rydberg Hamiltonian maps to the model as J1 = U1/4, Delta_x = Omega/2 and
Delta_z = (dU - delta)/2, so Delta_x/J1 = 2 Omega/U1 and Delta_z/J1 = 2 (dU - delta)/U1,
with dU = 1/2 sum_j U_ij averaged over all atoms (Eq. S4).

Every run is a JSON record of bitstring counts per sweep point, whatever made
it (QPU through QFw, local emulation, replayed data), and `analyze` turns any
of them into the paper's observables.

A run is three Slurm jobs (tmgo-pipeline.sh): `prepare` builds the sweep on the
device's public specs, `qfw` runs it through QFw, and `analyze` computes the observables.
--device-id names the QFw device (qfw-devices.yaml): pasqal-emu-free (Pasqal Cloud's EMU_FREE, on
FRESNEL_CAN1's specs, no detection errors, 3x3 by default), pasqal-fresnel-can1 (the FRESNEL_CAN1 QPU,
7x7 by default as the paper's smallest scan) or pasqal-fresnel (the FRESNEL QPU, 42 atoms at least).
The atoms sit at the spacing that gives the paper's Delta_x/J1 = 1.08, on a layout of their own.

Usage:
    tmgo.py prepare --device-id pasqal-emu-free --points 8 --shots 100 --out prepared.json
    tmgo.py qfw --device-id pasqal-emu-free --prepared prepared.json --out record.json   (in a QFw Slurm job)
    tmgo.py refine --record record.json --prepared prepared.json --points 4 --out refined.json
    tmgo.py analyze record.json [--plot fig.png]
    tmgo.py sim --device-id pasqal-fresnel --size 3 --out sim.json   (local emulation: QuTiP, emu-sv or emu-mps)
"""

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
from pulser import Pulse, Register, Sequence
from pulser.waveforms import ConstantWaveform, RampWaveform

RISE = 1000  # ns: Omega ramp-up at the initial detuning
Q13 = 2 * math.pi / 3 * np.array([1.0, math.sqrt(3)])  # 1/3-order Bragg peak, 1/a units
DX_J1 = 1.08  # the paper's transverse field, Omega/U1 = 0.54
# QFw device id: what runs the sweep, the Pasqal device whose public specs it is built on, default rhombus side
DEVICES = {"pasqal-emu-free": ("EMU_FREE emulator (Pasqal Cloud)", "FRESNEL_CAN1", 3),
	"pasqal-fresnel-can1": ("FRESNEL_CAN1 QPU", "FRESNEL_CAN1", 7), "pasqal-fresnel": ("FRESNEL QPU", "FRESNEL", 7)}


def rhombus_register(device, size):
	"""Place an L x L rhombus at the spacing where the channel's maximum Omega gives the paper's Delta_x/J1.

	The register gets a layout of its own, which the device calibrates: its 5 um calibrated layouts
	would give Delta_x/J1 = 0.45, and their detuning range would miss the low fields from 5x5 up.

	Args:
		device (pulser.devices.Device): Device that accepts new layouts.
		size (int): Rhombus side L.

	Returns:
		register (pulser.register.Register): Register with atoms named "m,n" by rhombus position.
		spacing (float): Lattice spacing in um.
	"""
	spacing = (device.interaction_coeff * DX_J1 / (2 * device.channels["rydberg_global"].max_amp)) ** (1 / 6)
	ids = [f"{i},{j}" for j in range(size) for i in range(size)]
	pos = lattice_positions(ids) * spacing
	return Register(dict(zip(ids, pos - pos.mean(0)))).with_automatic_layout(device), spacing


def bulk_ids(size, margin):
	"""Return the qubit ids at least `margin` sites from the rhombus edge."""
	return [f"{i},{j}" for j in range(margin, size - margin) for i in range(margin, size - margin)]


def lattice_positions(ids):
	"""Return the positions of the atoms named "m,n", in units of the lattice spacing."""
	mn = np.array([[int(x) for x in q.split(",")] for q in ids])
	return np.c_[mn[:, 0] + mn[:, 1] / 2, mn[:, 1] * math.sqrt(3) / 2]


def model_scale(pos, c6):
	"""Return U1 and dU of atoms at `pos`, in the units of `c6`.

	dU averages over all atoms, as the paper's Eq. (S4) does.
	"""
	dist = np.linalg.norm(pos[:, None] - pos[None], axis=-1)
	np.fill_diagonal(dist, np.inf)
	u = c6 / dist ** 6
	return u.max(), 0.5 * u.sum(1).mean()


def sweep_shape(device):
	"""Return Omega, the initial detuning and the sweep duration (ns) on the device."""
	channel = device.channels["rydberg_global"]
	return channel.max_amp, -channel.max_abs_detuning, (device.max_sequence_duration or 6000) - RISE


def build_sequence(device, register):
	"""Build the sweep sequence with the final detuning as the variable `delta_f`.

	Omega ramps to the channel maximum at the most negative detuning, the detuning then ramps
	linearly to delta_f over the rest of the device's maximum duration. As in the paper, the
	sequence ends there, at (Omega, delta_f): the device switches the light off.
	"""
	omega, delta0, sweep = sweep_shape(device)
	seq = Sequence(register, device)
	seq.declare_channel("rydberg", "rydberg_global")
	delta_f = seq.declare_variable("delta_f")
	seq.add(Pulse(RampWaveform(RISE, 0.0, omega), ConstantWaveform(RISE, delta0), 0.0), "rydberg")
	seq.add(Pulse.ConstantAmplitude(omega, RampWaveform(sweep, delta0, delta_f), 0.0), "rydberg")
	return seq


def sweep_points(dmax, u1, du, points):
	"""Return the final detunings within +-dmax, evenly spaced in Delta_z/J1."""
	lo, hi = 2 * (du - dmax) / u1, 2 * (du + dmax) / u1
	return [float(du - z * u1 / 2) for z in np.linspace(max(lo, 0.0), hi, points)]


def observables(record):
	"""Compute the paper's estimators per sweep point, corrected for detection errors.

	Uses SM Eqs. (S2)-(S3) for M^z_i and C^zz_ij with sigma^z = +1 on |g> (bit 0) and the
	structure factor S(q_1/3) from the corrected C^zz.
	Sites read "X" (atom not loaded) are left out of each average.

	Args:
		record (dict): Run record with metadata and `points`, each with `delta_f` and `counts`.

	Returns:
		(list): One dict per point with dz_j1, m, its standard error m_err and s_q13.
	"""
	eps, eps_ = record["p_false_pos"], record["p_false_neg"]
	ids = record["qubit_ids"]
	bulk = [ids.index(q) for q in bulk_ids(record["size"], record["margin"]) or ids]
	r = lattice_positions(ids)[bulk]
	phase = np.exp(1j * (r[:, None] - r[None]) @ Q13)
	rows = []
	for point in record["points"]:
		# raw sigma^z per shot and site, 0 where no atom was loaded
		z = np.array([[{"0": 1, "1": -1}.get(b, 0) for b in s] for s in point["counts"]])[:, bulk]
		n = np.abs(z)
		w = np.array(list(point["counts"].values()), float)
		mt = w @ z / (w @ n)
		ct = (z.T * w) @ z / ((n.T * w) @ n)
		m = (mt - (eps_ - eps)) / (1 - eps - eps_)
		# (S3) with +(eps' - eps)^2: sigma~ = a + b sigma per site gives C~ = b^2 C + a (M~_i + M~_j) - a^2
		c = (ct - (eps_ - eps) * (mt[:, None] + mt[None]) + (eps_ - eps) ** 2) / (1 - eps - eps_) ** 2
		np.fill_diagonal(c, 1.0)
		conn = c - np.outer(m, m)
		# shot-to-shot spread of the bulk magnetization, over shots with at least one bulk atom
		loaded = n.any(1)
		ms, wl = z[loaded].sum(1) / n[loaded].sum(1), w[loaded]
		m_err = math.sqrt(wl @ (ms - wl @ ms / wl.sum()) ** 2) / wl.sum() / (1 - eps - eps_)
		rows.append({"delta_f": point["delta_f"], "dz_j1": 2 * (record["du"] - point["delta_f"]) / record["u1"],
			"m": float(m.mean()), "m_err": m_err, "s_q13": float(np.real((phase * conn).sum()) / len(bulk)),
			"shots": int(w.sum())})
	return rows


def load_device(device_id):
	"""Return the Pulser device a sweep for `device_id` is built on, from Pasqal Cloud's public specs (no account)."""
	from urllib.request import urlopen
	from pulser.abstract_repr import deserialize_device
	data = json.load(urlopen("https://apis.pasqal.cloud/core-fast/api/v1/devices/public-specs", timeout=30))["data"]
	return deserialize_device({d["device_type"]: d["specs"] for d in data}[DEVICES[device_id][1]])


def prepare(device, args, source):
	"""Return the run record without points, the sweep sequence and its final detunings.

	An emulator adds no detection errors, so a sweep for one needs no correction.
	"""
	ideal = "emu" in args.device_id
	size = args.size or DEVICES[args.device_id][2]
	margin = (2 if size >= 7 else 0) if args.margin is None else args.margin  # the paper's bulk from 7x7 up
	register, spacing = rhombus_register(device, size)
	ids = list(register.qubit_ids)
	pos = np.array([register.qubits[q].as_array() for q in ids])
	u1, du = model_scale(pos, device.interaction_coeff)
	channel, noise = device.channels["rydberg_global"], device.noise_model
	record = {"device": device.name, "size": size, "margin": margin, "spacing_um": spacing,
		"qubit_ids": ids, "u1": u1, "du": du, "dx_j1": 2 * channel.max_amp / u1,
		"p_false_pos": 0.0 if ideal else noise.p_false_pos,
		"p_false_neg": 0.0 if ideal else noise.p_false_neg, "source": source, "shots": args.shots}
	points = sweep_points(channel.max_abs_detuning, u1, du, args.points)
	return record, build_sequence(device, register), points


def save(record, out):
	"""Write a run record as JSON."""
	Path(out).parent.mkdir(parents=True, exist_ok=True)
	Path(out).write_text(json.dumps(record, indent=1))


def emulate(args):
	"""Return a function sampling the final state of a built sequence with QuTiP, emu-sv or emu-mps."""
	if args.emulator == "qutip":
		from pulser_simulation import QutipEmulator
		return lambda seq: QutipEmulator.from_sequence(seq, sampling_rate=0.05).run().sample_final_state(
			N_samples=args.shots)
	from pulser.backend import BitStrings
	common = {"observables": [BitStrings(evaluation_times=[1.0], num_shots=args.shots)], "dt": 10, "log_level": 30}
	if args.emulator == "sv":
		from emu_sv import SVBackend as Backend, SVConfig
		config = SVConfig(**common)
	else:
		from emu_mps import MPSBackend as Backend, MPSConfig
		config = MPSConfig(max_bond_dim=args.bond_dim, **common)
	return lambda seq: Backend(seq, config=config).run().get_result("bitstrings", 1.0)


def simulate(args):
	"""Run the sweep on a local emulator, apply the device's detection errors and save it."""
	device = load_device(args.device_id)
	source = {"qutip": "QuTiP emulation", "sv": "emu-sv emulation"}.get(args.emulator,
		f"emu-mps emulation (bond dimension {args.bond_dim})")
	record, seq, points = prepare(device, args, source + (", no detection errors" if "emu" in args.device_id else ""))
	sample = emulate(args)
	rng = np.random.default_rng(args.seed)
	record["points"] = []
	for delta_f in points:
		start = time.monotonic()
		point = seq.build(delta_f=delta_f)
		counts = detect(sample(point), record["p_false_pos"], record["p_false_neg"], rng)
		record["points"].append({"delta_f": delta_f, "counts": counts})
		print(f"delta_f {delta_f:7.2f} rad/us  {time.monotonic() - start:5.1f}s", flush=True)
		save(record, args.out)


def prepare_run(args):
	"""Build the sweep on the device's public specs and save it, with its sequence, for `qfw`."""
	record, seq, points = prepare(load_device(args.device_id), args, None)
	save(record | {"pass": "coarse sweep", "sweep": points, "sequence": seq.to_abstract_repr()}, args.out)
	print(f"Prepared {len(points)} points x {args.shots} shots on {record['device']}, "
		f"{len(record['qubit_ids'])} atoms: {args.out}")


def refine(args):
	"""Prepare a second sweep with more points around the S(q_1/3) peak of a finished run.

	The new points lie strictly between the peak's two neighbouring points of the first run,
	where the magnetization jumps; the sequence and the register are those of the first run.
	"""
	record = json.loads(Path(args.record).read_text())
	rows = sorted(observables(record), key=lambda r: r["dz_j1"])
	k = max(range(len(rows)), key=lambda i: rows[i]["s_q13"])
	lo, hi = rows[max(k - 1, 0)], rows[min(k + 1, len(rows) - 1)]
	points = [float(v) for v in np.linspace(lo["delta_f"], hi["delta_f"], args.points + 2)[1:-1]]
	prepared = json.loads(Path(args.prepared).read_text())
	save(prepared | {"pass": "refined near the S(q1/3) peak", "sweep": points}, args.out)
	print(f"S(q1/3) peaks at Dz/J1 = {rows[k]['dz_j1']:.2f} ({rows[k]['s_q13']:.3f}): {args.points} more points "
		f"between Dz/J1 = {lo['dz_j1']:.2f} and {hi['dz_j1']:.2f}: {args.out}")


def pulser_batch(record, points, connection):
	"""Run the prepared sequence as one Pulser batch, a job per point.

	Returns:
		results (list): Each point's results, None for a point that has none.
		status (set): The batch status.
	"""
	import pulser
	batch = pulser.QPUBackend(Sequence.from_abstract_repr(record.pop("sequence")), connection=connection).run(
		[{"runs": record["shots"], "variables": {"delta_f": v}} for v in points], wait=True)
	got = batch.get_available_results()
	return [got.get(j) for j in batch.job_ids], {batch.get_batch_status().name}


def run_qfw(args, submit=pulser_batch):
	"""Run a prepared sweep through the QFw reservation of this Slurm job and save it.

	The QPM must serve the device the sweep was prepared on, or an emulator such as EMU_FREE.
	Pulser checks the sequence against the device's current specs before it submits the batch.
	The points that got results are saved even if others failed: their shots are paid for.
	"""
	import os
	import sys
	sys.path.insert(0, os.path.join(os.environ["QFW_EXAMPLES_DIR"], "tests"))
	from qfw_example_context import qfw_reservation_options
	from qfw_example_report import emit_result
	from qfw_pulser import QFwConnection
	record = json.loads(Path(args.prepared).read_text())
	record["source"] = f"{DEVICES[args.device_id][0]} through QFw and {args.lib.upper()}" + record.pop("written", "")
	points = record.pop("sweep")
	connection = QFwConnection(lib=args.lib, **qfw_reservation_options())
	start = time.monotonic()
	results, status = submit(record, points, connection)
	done = [(v, r) for v, r in zip(points, results) if r is not None]
	if not done:
		raise SystemExit(f"batch {'/'.join(sorted(status))}: no point has results")
	record |= {"seconds": round(time.monotonic() - start, 1), "batch_status": "/".join(sorted(status)),
		"qubit_ids": [str(q) for q in done[0][1].atom_order],
		"points": [{"delta_f": v, "counts": dict(r.bitstring_counts)} for v, r in done]}
	save(record, args.out)
	if status != {"DONE"} or len(done) != len(points):
		raise SystemExit(f"batch {'/'.join(sorted(status))}: saved the {len(done)} of {len(points)} points "
			f"with results to {args.out}")
	print(f"{record['source']}, built for {record['device']}: {record['batch_status']} in {record['seconds']}s")
	emit_result("tmgo", parameters={k: record[k] for k in ("device", "size", "shots", "dx_j1")},
		metrics={"observables": report(record)})


def report(record):
	"""Print the observables of a run record as a table and return them."""
	rows = observables(record)
	print(f"TmMgGaO4 field sweep: {record['source']}, built for {record['device']}, {record['size']}x{record['size']}, "
		f"Delta_x/J1 = {record['dx_j1']:.2f}, eps = {record['p_false_pos']}, eps' = {record['p_false_neg']}")
	print("  Dz/J1     M^z     +-  S(q1/3)")
	for row in rows:
		print(f"  {row['dz_j1']:5.2f}  {row['m']:6.3f}  {row['m_err']:5.3f}  {row['s_q13']:7.3f}")
	return rows


def detect(counts, eps, eps_, rng):
	"""Apply false positives (0 read as 1) and false negatives (1 read as 0) shot by shot."""
	out = {}
	for bits, n in counts.items():
		b = np.array([int(x) for x in bits])
		flip = rng.random((n, b.size)) < np.where(b == 1, eps_, eps)
		for shot in b ^ flip:
			key = "".join(map(str, shot))
			out[key] = out.get(key, 0) + 1
	return out


def load_records(files):
	"""Read run records and print the observables of each."""
	records = [json.loads(Path(f).read_text()) | {"file": f} for f in files]
	for rec in records:
		print(f"{rec['file']}: ", end="")
		rec["rows"] = report(rec)
	return records


def plot(rows):
	"""Plot M^z and S(q1/3) against Delta_z/J1, one row of panels per (title, records) pair.

	Returns:
		(matplotlib.figure.Figure): The figure, with axes (M^z, S) per row.
	"""
	import matplotlib.pyplot as plt
	fig, axes = plt.subplots(len(rows), 2, figsize=(11, 4.5 * len(rows)), squeeze=False, layout="constrained")
	for (ax_m, ax_s), (title, group) in zip(axes, rows):
		ax_m.axhline(1 / 3, color="gray", lw=0.8, ls="--", label="1/3 plateau")
		for rec in group:
			x = [r["dz_j1"] for r in rec["rows"]]
			label = f"{rec['source']}, {rec['size']}x{rec['size']} atoms" + (f", {rec['pass']}" if "pass" in rec else "")
			ax_m.errorbar(x, [r["m"] for r in rec["rows"]], yerr=[r["m_err"] for r in rec["rows"]], fmt="o-", ms=4, label=label)
			ax_s.plot(x, [r["s_q13"] for r in rec["rows"]], "o-", ms=4)
		for ax, name in ((ax_m, "magnetization $M^z$"), (ax_s, "three-sublattice order $S(q_{1/3})$")):
			ax.set_title(f"{title}: {name.split(' $')[0]}")
			ax.set_xlabel("longitudinal field $\\Delta_z/J_1$")
			ax.set_ylabel(name)
		ax_m.legend(loc="upper left", bbox_to_anchor=(0, -0.15), fontsize=8, frameon=False)
	fig.suptitle("TmMgGaO$_4$ frustrated magnet: field sweep")
	return fig


def analyze(args):
	"""Print the observables of each record and plot them against Delta_z/J1."""
	records = load_records(args.records)
	if args.plot:
		plot([("This run", records)]).savefig(args.plot, dpi=150)


def parser():
	"""Return the command line parser of tmgo.py."""
	parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	sub = parser.add_subparsers(dest="command", required=True)
	run = argparse.ArgumentParser(add_help=False)
	run.add_argument("--size", type=int, help="rhombus side L: 3 on the emulator, 7 on a QPU")
	run.add_argument("--margin", type=int, help="edge rows left out of the bulk: the paper's 2 from 7x7 up, else 0")
	run.add_argument("--points", type=int, default=8, help="8 x 100 shots: 800 shots in all")
	run.add_argument("--shots", type=int, default=100)
	run.add_argument("--out", required=True)
	run.add_argument("--device-id", choices=DEVICES, default="pasqal-emu-free", help="QFw device the sweep is for")
	sim = sub.add_parser("sim", parents=[run], help="emulate the sweep locally")
	sim.add_argument("--emulator", choices=("qutip", "sv", "mps"), default="qutip",
		help="sv: emu-sv, up to 4x4; mps: emu-mps, for 7x7")
	sim.add_argument("--bond-dim", type=int, default=32)
	sim.add_argument("--seed", type=int, default=0)
	sub.add_parser("prepare", parents=[run], help="build the sweep for `qfw` on the device's public specs")
	qfw = sub.add_parser("qfw", help="run a prepared sweep through QFw inside a Slurm job")
	qfw.add_argument("--prepared", required=True, help="output of `prepare`")
	qfw.add_argument("--out", required=True)
	qfw.add_argument("--lib", default="qrmi", help="shim library")
	qfw.add_argument("--device-id", choices=DEVICES, required=True, help="QFw device the QPM serves")
	ref = sub.add_parser("refine", help="prepare a second sweep around the S(q1/3) peak of a run")
	ref.add_argument("--record", required=True, help="the finished run")
	ref.add_argument("--prepared", required=True, help="output of `prepare` for that run")
	ref.add_argument("--points", type=int, default=4, help="new points")
	ref.add_argument("--out", required=True)
	ana = sub.add_parser("analyze", help="print and plot observables of run records")
	ana.add_argument("records", nargs="+")
	ana.add_argument("--plot")
	return parser


def main():
	args = parser().parse_args()
	{"sim": simulate, "prepare": prepare_run, "qfw": run_qfw, "refine": refine, "analyze": analyze}[args.command](args)


if __name__ == "__main__":
	main()
