"""Transparent grouping and ordering for multi-screen research candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .domain import Candidate, CandidateStatus


_OCC_CONTRACT = re.compile(r"[A-Z0-9.]{1,6}\d{6}[CP]\d{8}")


@dataclass(frozen=True, slots=True)
class RankedDiscovery:
    """One underlying/contract group with every screen result retained.

    `rank` is based on the count of distinct screens whose candidates reached
    research status. It is an ordering aid, not a probability or a blended
    score. Screen-specific scores remain on their original Candidate records.
    """

    rank: int
    discovery_id: str
    symbol: str
    contract_symbols: tuple[str, ...]
    candidates: tuple[Candidate, ...]
    supporting_screens: tuple[str, ...]
    suppressed_screens: tuple[str, ...]
    ranking_basis: str

    @property
    def research_screen_count(self) -> int:
        return len(self.supporting_screens)


def rank_discoveries(candidates: Iterable[Candidate]) -> list[RankedDiscovery]:
    """Group related screen records and rank by independent screen support.

    Candidates sharing an option contract are grouped together. Symbol-level
    candidates without a contract (for example, an overextended-equity result)
    are included as context in every contract group for that symbol. When no
    contract is identified, the symbol-level results form a symbol-only group.

    Duplicate candidate IDs are collapsed to the newest record. Every distinct
    research or suppressed record is retained in its resulting group.
    """
    records = _deduplicate(candidates)
    by_symbol: dict[str, list[tuple[Candidate, tuple[str, ...]]]] = {}
    for candidate in records:
        symbol = candidate.symbol.strip().upper() or "UNKNOWN"
        contracts = _contract_symbols(candidate)
        by_symbol.setdefault(symbol, []).append((candidate, contracts))

    groups: list[tuple[str, tuple[str, ...], tuple[Candidate, ...]]] = []
    for symbol in sorted(by_symbol):
        symbol_records = by_symbol[symbol]
        contexts = [candidate for candidate, contracts in symbol_records if not contracts]
        contract_records = [
            (candidate, contracts)
            for candidate, contracts in symbol_records
            if contracts
        ]
        components = _contract_components(contract_records)
        if not components:
            if contexts:
                groups.append((symbol, (), tuple(contexts)))
            continue
        for contracts, related in components:
            # The same underlying-level context is applicable to each contract
            # analysis, while all option-specific evidence stays grouped by ID.
            combined = _deduplicate([*contexts, *related])
            groups.append((symbol, contracts, tuple(combined)))

    prepared = []
    for symbol, contracts, related in groups:
        supporting = tuple(sorted({
            candidate.scanner
            for candidate in related
            if candidate.status == CandidateStatus.RESEARCH
            and candidate.scanner not in {"contract_selection", "event_catalyst"}
        }))
        suppressed = tuple(sorted({
            candidate.scanner
            for candidate in related
            if candidate.status == CandidateStatus.SUPPRESSED
        }))
        prepared.append((symbol, contracts, related, supporting, suppressed))

    # Screen-specific scores are deliberately excluded: a robust volume score,
    # IV/RV divergence, and earnings divergence do not share a calibrated scale.
    prepared.sort(key=lambda row: (
        -len(row[3]),
        *_contract_selection_quality(row[2]),
        -_latest_research_timestamp(row[2]).timestamp(),
        row[0],
        row[1],
    ))

    result: list[RankedDiscovery] = []
    for index, (symbol, contracts, related, supporting, suppressed) in enumerate(prepared, start=1):
        if supporting:
            basis = (
                f"{len(supporting)} research screen(s) met their configured thresholds: "
                f"{', '.join(supporting)}. Screen scores remain separate. "
                "Ties favor the eligible pair nearest the 30-DTE target, then nearest ATM."
            )
        else:
            basis = "No research screen met its configured threshold; retained for audit."
            if any(candidate.scanner == "contract_selection" for candidate in related):
                basis += (
                    " Eligible pairs are ordered by distance from 30 DTE, "
                    "then distance from ATM."
                )
        contract_label = "+".join(contracts) if contracts else "underlying"
        result.append(RankedDiscovery(
            rank=index,
            discovery_id=f"{symbol}:discovery:{contract_label}",
            symbol=symbol,
            contract_symbols=contracts,
            candidates=related,
            supporting_screens=supporting,
            suppressed_screens=suppressed,
            ranking_basis=basis,
        ))
    return result


def _deduplicate(candidates: Iterable[Candidate]) -> list[Candidate]:
    by_id: dict[str, Candidate] = {}
    for candidate in candidates:
        prior = by_id.get(candidate.candidate_id)
        if prior is None or _candidate_preference(candidate) > _candidate_preference(prior):
            by_id[candidate.candidate_id] = candidate
    return sorted(
        by_id.values(),
        key=lambda candidate: (
            candidate.symbol.upper(),
            candidate.scanner,
            candidate.candidate_id,
        ),
    )


def _candidate_preference(candidate: Candidate) -> tuple[float, int]:
    created_at = candidate.created_at
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        timestamp = float("-inf")
    else:
        timestamp = created_at.astimezone(timezone.utc).timestamp()
    research = int(candidate.status == CandidateStatus.RESEARCH)
    return timestamp, research


def _contract_symbols(candidate: Candidate) -> tuple[str, ...]:
    found: set[str] = set()
    metric_contract = candidate.metrics.get("contract_symbol")
    if isinstance(metric_contract, str) and _OCC_CONTRACT.fullmatch(metric_contract.upper()):
        found.add(metric_contract.upper())
    for input_id in candidate.input_ids:
        token = str(input_id).upper()
        if _OCC_CONTRACT.fullmatch(token):
            found.add(token)
    return tuple(sorted(found))


def _contract_components(
    rows: list[tuple[Candidate, tuple[str, ...]]],
) -> list[tuple[tuple[str, ...], list[Candidate]]]:
    """Return connected components where candidates share any option leg."""
    parents = list(range(len(rows)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    owner: dict[str, int] = {}
    for index, (_candidate, contracts) in enumerate(rows):
        for contract in contracts:
            if contract in owner:
                left, right = root(index), root(owner[contract])
                if left != right:
                    parents[right] = left
            else:
                owner[contract] = index

    components: dict[int, list[int]] = {}
    for index in range(len(rows)):
        components.setdefault(root(index), []).append(index)

    output = []
    for indexes in components.values():
        contracts = tuple(sorted({
            contract
            for index in indexes
            for contract in rows[index][1]
        }))
        related = _deduplicate(rows[index][0] for index in indexes)
        output.append((contracts, related))
    return sorted(output, key=lambda component: component[0])


def _latest_research_timestamp(candidates: tuple[Candidate, ...]) -> datetime:
    timestamps = [
        candidate.created_at.astimezone(timezone.utc)
        for candidate in candidates
        if candidate.status == CandidateStatus.RESEARCH
        and candidate.created_at.tzinfo is not None
        and candidate.created_at.utcoffset() is not None
    ]
    return max(timestamps, default=datetime.min.replace(tzinfo=timezone.utc))


def _contract_selection_quality(candidates: tuple[Candidate, ...]) -> tuple[float, float]:
    """Prefer a selected pair near the target expiry and underlying price."""
    selected = next((
        candidate for candidate in candidates
        if candidate.scanner == "contract_selection"
        and candidate.status == CandidateStatus.RESEARCH
    ), None)
    if selected is None:
        return float("inf"), float("inf")
    dte = selected.metrics.get("option_dte")
    moneyness = selected.metrics.get("moneyness_deviation_fraction")
    try:
        dte_distance = abs(float(dte) - 30)
        moneyness_distance = float(moneyness)
        if moneyness_distance < 0:
            raise ValueError
        return dte_distance, moneyness_distance
    except (TypeError, ValueError):
        return float("inf"), float("inf")
