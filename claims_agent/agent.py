"""Insurance claim processing as an ADK graph workflow.

START -> intake (LLM) -> validate_policy
    validate_policy --ok-----------> [fraud_check | damage_assessment | coverage_check] -> join -> decide
    validate_policy --missing_docs-> request_docs (human) -> validate_policy   (loop, max 2)
    validate_policy --not_covered--> reject
    decide --auto------> settle
    decide --adjuster--> adjuster_review (human) --approved--> settle / --denied--> reject
    decide --siu-------> siu_referral
    settle | reject -> letter_writer (LLM)
"""

from typing import Any

from google.adk import Agent, Context, Event, Workflow
from google.adk.events.request_input import RequestInput
from google.adk.workflow import JoinNode
from pydantic import BaseModel, Field

from .data import CLAIM_HISTORY, POLICIES, REQUIRED_DOCS, SUSPICIOUS_KEYWORDS

MODEL = "gemini-3.8-flash"
AUTO_APPROVE_LIMIT = 5000
MAX_DOC_REQUESTS = 2


class ClaimData(BaseModel):
    policy_id: str = Field(description="Policy id such as POL-1001")
    incident_type: str = Field(
        description="One of: auto_collision, theft, water_damage, fire"
    )
    claimed_amount: float = Field(description="Amount claimed in USD")
    description: str = Field(description="Short summary of what happened")
    documents: list[str] = Field(
        default_factory=list,
        description="Attached documents, from: police_report, photos, plumber_invoice, fire_dept_report",
    )


class DamageEstimate(BaseModel):
    estimated_cost: float = Field(description="Realistic repair/replacement cost in USD")
    reasoning: str


# --- Nodes ---

intake = Agent(
    name="intake",
    model=MODEL,
    instruction=(
        "Extract the insurance claim details from the user's message into the "
        "required schema. Only list documents the claimant explicitly says are attached."
    ),
    output_schema=ClaimData,
)


def validate_policy(ctx: Context, node_input: Any):
    claim = ClaimData.model_validate(node_input)
    policy = POLICIES.get(claim.policy_id)
    attempts = ctx.state.get("doc_requests", 0)

    if not policy or not policy["active"]:
        reason = "Policy not found" if not policy else "Policy is not active"
        yield Event(output={"reason": reason, "claim": claim.model_dump()}, route="not_covered")
        return
    if claim.incident_type not in policy["covers"] or claim.incident_type not in REQUIRED_DOCS:
        yield Event(
            output={"reason": f"'{claim.incident_type}' is not covered by this policy", "claim": claim.model_dump()},
            route="not_covered",
        )
        return

    missing = [d for d in REQUIRED_DOCS[claim.incident_type] if d not in claim.documents]
    if missing and attempts < MAX_DOC_REQUESTS:
        yield Event(
            output={"claim": claim.model_dump(), "missing": missing},
            state={"claim": claim.model_dump(), "missing": missing},
            route="missing_docs",
        )
        return
    if missing:
        yield Event(
            output={"reason": f"Required documents not provided: {', '.join(missing)}", "claim": claim.model_dump()},
            route="not_covered",
        )
        return

    yield Event(output=claim.model_dump(), state={"claim": claim.model_dump()}, route="ok")


def request_docs(ctx: Context):
    attempts = ctx.state.get("doc_requests", 0)
    yield RequestInput(
        interrupt_id=f"docs-{attempts}",
        message=f"Missing documents: {', '.join(ctx.state['missing'])}. "
        "Reply with the document names you are now providing.",
        response_schema={"type": "object", "properties": {"documents": {"type": "array", "items": {"type": "string"}}}},
    )


def merge_docs(ctx: Context, node_input: Any):
    # node_input is the human's reply to request_docs
    claim = ClaimData.model_validate(ctx.state["claim"])
    provided = node_input.get("documents", []) if isinstance(node_input, dict) else []
    claim.documents = sorted(set(claim.documents) | set(provided))
    yield Event(output=claim.model_dump(), state={"doc_requests": ctx.state.get("doc_requests", 0) + 1})



def fraud_check(node_input: Any) -> dict:
    claim = ClaimData.model_validate(node_input)
    score = 0.0
    signals = []
    recent = [c for c in CLAIM_HISTORY.get(claim.policy_id, []) if c["days_ago"] <= 180]
    if len(recent) >= 2:
        score += 0.5
        signals.append(f"{len(recent)} claims in the last 180 days")
    text = claim.description.lower()
    hits = [k for k in SUSPICIOUS_KEYWORDS if k in text]
    if hits:
        score += 0.2 * len(hits)
        signals.append(f"suspicious phrasing: {', '.join(hits)}")
    score = min(score, 1.0)
    level = "high" if score >= 0.7 else "medium" if score >= 0.3 else "low"
    return {"score": round(score, 2), "level": level, "signals": signals}


damage_assessment = Agent(
    name="damage_assessment",
    model=MODEL,
    instruction=(
        "You are a claims adjuster. Given the claim, estimate a realistic repair or "
        "replacement cost in USD. Be conservative and explain briefly."
    ),
    output_schema=DamageEstimate,
)


def coverage_check(node_input: Any) -> dict:
    claim = ClaimData.model_validate(node_input)
    policy = POLICIES[claim.policy_id]
    return {"limit": policy["limit"], "deductible": policy["deductible"]}


join = JoinNode(name="join")


def decide(node_input: dict):
    fraud = node_input["fraud_check"]
    damage = node_input["damage_assessment"]
    cover = node_input["coverage_check"]
    payout = max(min(damage["estimated_cost"], cover["limit"]) - cover["deductible"], 0)
    summary = {"fraud": fraud, "damage": damage, "coverage": cover, "payout": payout}

    if fraud["level"] == "high":
        route = "siu"
    elif fraud["level"] == "medium" or payout > AUTO_APPROVE_LIMIT:
        route = "adjuster"
    else:
        route = "auto"
    yield Event(output=summary, state={"decision": summary}, route=route)


def adjuster_review(ctx: Context):
    s = ctx.state["decision"]
    yield RequestInput(
        interrupt_id="adjuster",
        message=(
            f"Adjuster approval needed. Proposed payout ${s['payout']:,.0f} "
            f"(damage estimate ${s['damage']['estimated_cost']:,.0f}, fraud risk "
            f"{s['fraud']['level']}: {s['fraud']['signals']}). Approve?"
        ),
        response_schema={"type": "object", "properties": {"approved": {"type": "boolean"}, "note": {"type": "string"}}},
    )


def adjuster_decision(ctx: Context, node_input: Any):
    # node_input is the adjuster's reply to adjuster_review
    reply = node_input if isinstance(node_input, dict) else {}
    yield Event(
        output=ctx.state["decision"] | {"adjuster_note": reply.get("note", "")},
        route="approved" if reply.get("approved") else "denied",
    )



def settle(ctx: Context, node_input: Any):
    s = node_input
    return {"outcome": "APPROVED", "payout": s["payout"], "claim": ctx.state["claim"], "note": s.get("adjuster_note", "")}


def reject(ctx: Context, node_input: Any):
    reason = node_input.get("reason") or node_input.get("adjuster_note") or "Denied by adjuster"
    return {"outcome": "REJECTED", "reason": reason, "claim": ctx.state.get("claim")}


def siu_referral(ctx: Context, node_input: Any):
    s = node_input
    return (
        f"CLAIM REFERRED TO SPECIAL INVESTIGATIONS UNIT. Fraud score {s['fraud']['score']} "
        f"({', '.join(s['fraud']['signals'])}). No payout until the investigation completes."
    )


letter_writer = Agent(
    name="letter_writer",
    model=MODEL,
    instruction=(
        "Write a short, empathetic letter (under 120 words) to the policyholder about "
        "their claim outcome. Include the payout amount or the reason for rejection."
    ),
)

root_agent = Workflow(
    name="adk_insurance_claims_workflow",
    edges=[
        ("START", intake, validate_policy),
        (
            validate_policy,
            {
                "ok": (fraud_check, damage_assessment, coverage_check),
                "missing_docs": request_docs,
                "not_covered": reject,
            },
        ),
        (request_docs, merge_docs, validate_policy),
        ((fraud_check, damage_assessment, coverage_check), join, decide),
        (
            decide,
            {"auto": settle, "adjuster": adjuster_review, "siu": siu_referral},
        ),
        (adjuster_review, adjuster_decision, {"approved": settle, "denied": reject}),
        (settle, letter_writer),
        (reject, letter_writer),
    ],
)
