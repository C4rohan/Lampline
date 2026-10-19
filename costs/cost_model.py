#!/usr/bin/env python3
"""Monthly cost model: local vs. Google Cloud deployments of the voice platform.

Run:  python3 costs/cost_model.py > costs/COSTS.md
Every price is a list price checked 2026-10-17 (sources in PRICES / COSTS.md).
Every capacity number is an ASSUMPTION to replace with load-test results.
"""
import math

# ---------------------------------------------------------------- prices (USD)
PRICES = {
    # Google Cloud (us-central1 / default list rates; us-east4 may differ slightly)
    "run_vcpu_s": 0.000018,          # Cloud Run instance-based, per vCPU-second
    "run_gib_s": 0.000002,           # Cloud Run instance-based, per GiB-second
    "run_free_vcpu_s": 240_000,      # monthly free tier (instance-based)
    "run_free_gib_s": 450_000,
    "gke_cluster_hr": 0.10,          # Autopilot cluster fee (a $74.40/mo free credit may offset it)
    "gke_vcpu_hr": 0.0445,           # Autopilot pod vCPU-hour
    "gke_gib_hr": 0.0049225,         # Autopilot pod GiB-hour
    "sql_g1_small_mo": 25.55,        # Cloud SQL db-g1-small (730 h)
    "sql_vcpu_hr_ha": 0.0826,        # Cloud SQL Enterprise HA vCPU-hour
    "sql_gib_hr_ha": 0.014,          # Cloud SQL Enterprise HA GiB-hour
    "sql_ssd_gb_mo": 0.17,
    "sql_ssd_gb_mo_ha": 0.34,
    "redis_basic_gib_hr": 0.049,     # Memorystore M1 Basic (Iowa)
    "redis_std_gib_hr": 0.064,       # Memorystore M1 Standard (Iowa)
    "lb_rule_hr": 0.025,             # external ALB forwarding rule
    "armor_mo": 7.00,                # Cloud Armor: ~$5/policy + ~$1/rule (approx.)
    "nat_ip_hr": 0.005,
    "egress_gib": 0.12,              # internet egress, North America, first TiB
    "gcs_gb_mo": 0.02,               # Standard storage (approx.)
    "l4_hr_ondemand": 0.853624,      # g2-standard-8 (1x L4) on-demand
    "l4_hr_cud3": 0.384131,          # g2-standard-8, 3-yr resource CUD
    # Model APIs
    "chirp_stt_min": 0.016,          # Google Chirp 3 real-time
    "deepgram_stt_min": 0.0077,      # Deepgram Nova-3 streaming PAYG (regular rate)
    "flashlite_in": 0.10, "flashlite_cached": 0.01, "flashlite_out": 0.40,  # Gemini 2.5 Flash-Lite /1M tok
    "haiku_in": 0.10, "haiku_cached": 0.01, "haiku_out": 0.50,              # Claude Haiku 5.5 /1M tok
    "chirp_hd_tts_mchar": 30.0,      # Google Chirp 3 HD voices /1M chars
    "fish_tts_mchar": 15.0,          # Fish Audio /1M UTF-8 bytes (~chars for en/es)
    "cxas_session": 0.50,            # CX Agent Studio voice session (first 5 min)
    "cxas_overage_min": 0.15,        # $0.0025/s after 5 min
    # Local hardware (October 2026 street prices are inflated by AI demand)
    "rtx5090": 6900.0,               # current lowest-average listing; 4-month typical ~$4,787
    "gpu_box_rest": 3000.0,          # CPU, 128 GB RAM, NVMe, 1600 W PSU, chassis (assumption)
    "cpu_server": 2500.0,            # no-GPU server for the engine (assumption)
    "power_kwh": 0.15,               # commercial electricity (assumption)
}

# ------------------------------------------------------------- assumptions
CALL_MIN = 4.0                 # average handle time
BUSINESS_HOURS_MO = 22 * 10    # calls concentrated in 10 h x 22 days
PEAK_FACTOR = 1.5              # peak hour vs. average business hour
VCPU_PER_CALL = 0.25           # the engine's Pipecat pipeline per live call with cloud APIs (assumption)
CALLS_PER_GPU = 10             # STT+LLM(8B)+TTS on one L4 / RTX 5090 (assumption - benchmark!)
AMORTIZE_MONTHS = 36
AUDIO_MB_PER_MIN = 1.0         # stored recording (8 kHz 16-bit mono WAV ~0.96 MB/min)
EGRESS_MB_PER_MIN = 0.5        # audio sent back to the carrier (64 kbps mu-law)
RETENTION_MONTHS = 1
P = PRICES


def peak_concurrency(calls):
    avg = calls * CALL_MIN / (BUSINESS_HOURS_MO * 60)
    return max(1, math.ceil(avg * PEAK_FACTOR))


def avg_business_concurrency(calls):
    return calls * CALL_MIN / (BUSINESS_HOURS_MO * 60)


# ------------------------------------------------------- per-call model APIs
def llm_cost(mins, pin, pcache, pout, sys_tok=6000, turn_s=15, hist=150, out=60):
    turns = int(mins * 60 / turn_s)
    return sum(sys_tok * pcache / 1e6 + (hist * (k - 1) + 80) * pin / 1e6 + out * pout / 1e6
               for k in range(1, turns + 1))


TTS_CHARS_PER_MIN = 450  # agent speaks ~half the call


def api_per_call(stack):
    m = CALL_MIN
    if stack == "google":
        return (P["chirp_stt_min"] * m
                + llm_cost(m, P["flashlite_in"], P["flashlite_cached"], P["flashlite_out"])
                + P["chirp_hd_tts_mchar"] * TTS_CHARS_PER_MIN * m / 1e6)
    if stack == "mixed":
        return (P["deepgram_stt_min"] * m
                + llm_cost(m, P["haiku_in"], P["haiku_cached"], P["haiku_out"])
                + P["fish_tts_mchar"] * TTS_CHARS_PER_MIN * m / 1e6)
    if stack == "selfhosted":
        return 0.0
    raise ValueError(stack)


def cxas(calls):
    per = P["cxas_session"] + max(0.0, CALL_MIN - 5) * P["cxas_overage_min"]
    return calls * per


# ------------------------------------------------------- shared cloud extras
def storage_and_egress(calls):
    stored_gb = calls * CALL_MIN * AUDIO_MB_PER_MIN * RETENTION_MONTHS / 1024
    egress_gib = calls * CALL_MIN * EGRESS_MB_PER_MIN / 1024
    return stored_gb * P["gcs_gb_mo"] + egress_gib * P["egress_gib"]


# ------------------------------------------------------------ deployments
def cloud_run_poc(calls, stack):
    vcpu, gib = 2, 4
    calls_per_inst = int(vcpu / VCPU_PER_CALL)
    extra = max(0, math.ceil(avg_business_concurrency(calls) * PEAK_FACTOR / calls_per_inst) - 1)
    inst_seconds = 730 * 3600 + extra * BUSINESS_HOURS_MO * 3600
    compute = (max(0, inst_seconds * vcpu - P["run_free_vcpu_s"]) * P["run_vcpu_s"]
               + max(0, inst_seconds * gib - P["run_free_gib_s"]) * P["run_gib_s"])
    infra = (compute + 2.0  # ui, request-based, mostly idle
             + P["sql_g1_small_mo"] + 20 * P["sql_ssd_gb_mo"]
             + P["redis_basic_gib_hr"] * 730
             + P["lb_rule_hr"] * 730 + P["armor_mo"]
             + 5.0)  # secrets, registry, logs (approx.)
    return infra + storage_and_egress(calls), calls * api_per_call(stack)


def gke_prod(calls, stack, gpu=False):
    pod_cpu, pod_gib = 1, 2
    calls_per_pod = int(pod_cpu / VCPU_PER_CALL)
    peak = peak_concurrency(calls)
    web_pods_peak = max(2, math.ceil(peak / calls_per_pod))
    pod_hr = pod_cpu * P["gke_vcpu_hr"] + pod_gib * P["gke_gib_hr"]
    web = 2 * 730 * pod_hr + max(0, web_pods_peak - 2) * BUSINESS_HOURS_MO * pod_hr
    others = 730 * (0.25 * P["gke_vcpu_hr"] + 0.5 * P["gke_gib_hr"])            # arq worker
    others += 2 * 730 * (0.25 * P["gke_vcpu_hr"] + 0.5 * P["gke_gib_hr"])       # ui x2
    infra = (web + others + P["gke_cluster_hr"] * 730
             + 2 * 730 * P["sql_vcpu_hr_ha"] + 8 * 730 * P["sql_gib_hr_ha"] + 50 * P["sql_ssd_gb_mo_ha"]
             + P["redis_std_gib_hr"] * 730
             + P["lb_rule_hr"] * 730 + P["armor_mo"]
             + 3 * 730 * 0.0014 + P["nat_ip_hr"] * 730
             + 10.0)  # secrets, registry, logs, monitoring (approx.)
    if gpu:
        gpus = math.ceil(peak / CALLS_PER_GPU) + 1  # N+1 so one GPU can fail
        infra += gpus * 730 * P["l4_hr_cud3"]
    return infra + storage_and_egress(calls), calls * api_per_call(stack)


def onprem_cpu(calls, stack):
    hw = P["cpu_server"] * 2 / AMORTIZE_MONTHS            # two boxes for failover
    power = 2 * 0.15 * 730 * P["power_kwh"]               # ~150 W each
    return hw + power, calls * api_per_call(stack)


def onprem_gpu(calls):
    peak = peak_concurrency(calls)
    boxes = math.ceil(peak / CALLS_PER_GPU) + 1           # N+1
    capex = boxes * (P["rtx5090"] + P["gpu_box_rest"])
    monthly = capex / AMORTIZE_MONTHS + boxes * 0.6 * 730 * P["power_kwh"]  # ~600 W average
    return monthly, 0.0, boxes, capex


# ------------------------------------------------------------------ report
def money(x):
    return f"${x:,.0f}"


def main():
    volumes = [10_000, 50_000, 200_000]
    print("# Lampline: local vs. cloud cost comparison\n")
    print(f"_Generated by `costs/cost_model.py`. List prices checked 2026-10-17. "
          f"Average call {CALL_MIN:g} min. Monthly USD._\n")

    print("## Per-call model cost (APIs)\n")
    print("| Model stack | Per call | Per minute |")
    print("|---|---|---|")
    for name, key in [("All-Google: Chirp 3 STT + Gemini 2.5 Flash-Lite + Chirp 3 HD voice", "google"),
                      ("Mixed: Deepgram Nova-3 + Claude Haiku 5.5 + Fish Audio", "mixed"),
                      ("Self-hosted: Speaches/Whisper + Ollama 8B + Kokoro (GPU cost only)", "selfhosted")]:
        c = api_per_call(key)
        print(f"| {name} | ${c:.3f} | ${c / CALL_MIN:.4f} |")
    print(f"| CX Agent Studio (list, for reference) | ${cxas(1):.2f} | ${cxas(1) / CALL_MIN:.3f} |\n")

    print("## Monthly total by deployment\n")
    header = "| Deployment | " + " | ".join(f"{v // 1000}k calls/mo" for v in volumes) + " |"
    print(header)
    print("|---|" + "---|" * len(volumes))

    rows = []
    rows.append(("CX Agent Studio (list price, baseline)", [cxas(v) for v in volumes]))
    rows.append(("Local: on-prem server (no GPU) + Google model APIs",
                 [sum(onprem_cpu(v, "google")) for v in volumes]))
    rows.append(("Local: on-prem GPU servers + self-hosted models",
                 [onprem_gpu(v)[0] for v in volumes]))
    rows.append(("Cloud (coming soon): Cloud Run PoC + Google model APIs",
                 [sum(cloud_run_poc(v, "google")) for v in volumes]))
    rows.append(("Cloud (coming soon): GKE production + Google model APIs",
                 [sum(gke_prod(v, "google")) for v in volumes]))
    rows.append(("Cloud (coming soon): GKE production + mixed-vendor APIs",
                 [sum(gke_prod(v, "mixed")) for v in volumes]))
    rows.append(("Cloud (coming soon): GKE production + self-hosted models on L4 GPUs (3-yr CUD)",
                 [sum(gke_prod(v, "selfhosted", gpu=True)) for v in volumes]))
    for name, vals in rows:
        print(f"| {name} | " + " | ".join(money(x) for x in vals) + " |")

    print("\n## Effective cost per call\n")
    print(header)
    print("|---|" + "---|" * len(volumes))
    for name, vals in rows:
        print(f"| {name} | " + " | ".join(f"${x / v:.3f}" for x, v in zip(vals, volumes)) + " |")

    print("\n## Fixed vs. variable split (GKE production + Google APIs)\n")
    print("| Calls/mo | Platform infrastructure | Model APIs | Peak concurrent calls |")
    print("|---|---|---|---|")
    for v in volumes:
        infra, api = gke_prod(v, "google")
        print(f"| {v:,} | {money(infra)} | {money(api)} | {peak_concurrency(v)} |")

    print("\n## Local GPU option: up-front hardware\n")
    print("| Calls/mo | Peak concurrent | GPU servers (N+1) | Up-front cost |")
    print("|---|---|---|---|")
    for v in volumes:
        _, _, boxes, capex = onprem_gpu(v)
        print(f"| {v:,} | {peak_concurrency(v)} | {boxes} | {money(capex)} |")

    print(f"""
## Assumptions to replace with real numbers

- Average call {CALL_MIN:g} min; traffic in {BUSINESS_HOURS_MO} business hours/month with a {PEAK_FACTOR}x peak hour.
- The engine needs ~{VCPU_PER_CALL} vCPU per live call when models are remote APIs (an estimate for a Pipecat pipeline until load-tested).
- One L4 or RTX 5090 serves ~{CALLS_PER_GPU} concurrent calls running Whisper + an 8B LLM + Kokoro. This is the biggest unknown: benchmark it.
- Local hardware amortized over {AMORTIZE_MONTHS} months at October 2026 street prices (RTX 5090 ~${P['rtx5090']:,.0f}; it was ~$4,787 over the prior 4 months), electricity ${P['power_kwh']}/kWh.
- Not included anywhere: engineering and on-call staff, carrier/CCaaS charges, your network/security work, colocation or rack space for on-prem, BAAs, and support contracts. Staff cost dominates every option except CX Agent Studio.
- CX Agent Studio row is list price; large customers usually negotiate a lower rate.
- Cloud prices are list rates for us-central1 where us-east4 wasn't published; committed-use discounts would lower the GKE and Cloud SQL lines.
""")


if __name__ == "__main__":
    main()
