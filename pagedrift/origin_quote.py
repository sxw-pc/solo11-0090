"""Exact rational regional-content bounds for internal synthetic duty quotes.

The finished good's regional share is constrained by a rational
bill-of-materials fixed-point system, unit-interval share bounds and
independent evidence constraints. Cyclic strongly connected components (for
example two items that reference each other at 100%) are solved as one global
linear system over the rationals: cycles are never collapsed by an arbitrary
topological order and never rejected as malformed input. Bounds are proven
over the whole feasible region with dual certificates, so a joint constraint
such as X+Z<=1 is honoured globally instead of per component.

Both quote contracts are internal scenario instruments for fictional goods and
rates. They are not customs documents and state no legal conclusion.
"""
from __future__ import annotations

import json
from fractions import Fraction
from math import floor
from pathlib import Path
from typing import Any

from .ids import canonical_json, sha256_bytes


class OriginQuoteError(ValueError):
    pass


_QUOTE_CONTRACTS = ["guaranteed_origin_quote", "plausible_origin_quote"]


def _fraction(value: Any, what: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise OriginQuoteError(f"invalid {what}")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise OriginQuoteError(f"invalid {what}") from exc


def _validate_catalog(catalog: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise OriginQuoteError("unsupported catalog identity")
    run_id = catalog.get("run_id")
    audit_fingerprint = catalog.get("audit_fingerprint")
    catalog_fingerprint = catalog.get("catalog_fingerprint")
    offers = catalog.get("offers")
    if (not isinstance(run_id, str) or not run_id
            or not isinstance(audit_fingerprint, str) or not audit_fingerprint
            or not isinstance(catalog_fingerprint, str) or not catalog_fingerprint
            or not isinstance(offers, list) or not offers):
        raise OriginQuoteError("unsupported catalog identity")
    by_item: dict[str, dict[str, Any]] = {}
    for offer in offers:
        if not isinstance(offer, dict):
            raise OriginQuoteError("catalog offer must be an object")
        item_id = offer.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise OriginQuoteError("catalog offer has invalid item identity")
        if item_id in by_item:
            raise OriginQuoteError("catalog offers contain duplicate item identity")
        if _fraction(offer.get("price_cny"), "offer price") <= 0:
            raise OriginQuoteError("catalog offer has invalid price")
        by_item[item_id] = offer
    body = {
        "schema_version": 1,
        "run_id": run_id,
        "audit_fingerprint": audit_fingerprint,
        "offers": offers,
    }
    if sha256_bytes(canonical_json(body)) != catalog_fingerprint:
        raise OriginQuoteError("catalog identity drift detected")
    return by_item


def _parse_constraint(raw: Any, offers: dict[str, Any]) -> tuple[list[tuple[str, Fraction]], str, Fraction]:
    if not isinstance(raw, dict) or raw.get("op") not in ("<=", ">=", "=="):
        raise OriginQuoteError("invalid independent evidence constraint")
    right = _fraction(raw.get("right"), "constraint bound")
    terms = raw.get("terms")
    parsed: list[tuple[str, Fraction]] = []
    if terms is None:
        left = raw.get("left")
        if left not in offers:
            raise OriginQuoteError("constraint references unknown item")
        parsed.append((left, Fraction(1)))
    elif isinstance(terms, list) and terms:
        for term in terms:
            if not isinstance(term, dict) or term.get("item_id") not in offers:
                raise OriginQuoteError("constraint references unknown item")
            parsed.append((term["item_id"], _fraction(term.get("coefficient"), "constraint coefficient")))
    else:
        raise OriginQuoteError("invalid independent evidence constraint")
    return parsed, raw["op"], right


def _read_contract(contract_path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OriginQuoteError("cannot read origin contract") from exc
    if not isinstance(data, dict):
        raise OriginQuoteError("unsupported origin contract")
    return data


def _validate_contract(contract: dict[str, Any], offers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if contract.get("schema_version") != 1:
        raise OriginQuoteError("unsupported origin contract")
    finished = contract.get("finished_item_id")
    if finished not in offers:
        raise OriginQuoteError("finished item is not in catalog")
    threshold = _fraction(contract.get("regional_threshold"), "regional threshold")
    standard_rate = _fraction(contract.get("standard_duty_rate"), "standard duty rate")
    preference_rate = _fraction(contract.get("preference_duty_rate"), "preference duty rate")
    if not Fraction(0) <= threshold <= Fraction(1) or standard_rate < 0 or preference_rate < 0:
        raise OriginQuoteError("invalid threshold or duty rate")
    if contract.get("contracts") != _QUOTE_CONTRACTS:
        raise OriginQuoteError("unsupported quote contracts")
    terminals = contract.get("terminal_shares")
    bom = contract.get("bill_of_materials")
    if (not isinstance(terminals, dict) or not isinstance(bom, dict)
            or set(terminals) & set(bom) or set(terminals) | set(bom) != set(offers)):
        raise OriginQuoteError("origin graph does not partition the catalog")
    terminal_values = {}
    for item, value in terminals.items():
        share = _fraction(value, "terminal share")
        if not Fraction(0) <= share <= Fraction(1):
            raise OriginQuoteError("terminal share outside unit interval")
        terminal_values[item] = share
    graph: dict[str, list[tuple[str, Fraction]]] = {}
    for item, rows in bom.items():
        if not isinstance(rows, list) or not rows:
            raise OriginQuoteError("empty bill of materials")
        pairs = []
        for row in rows:
            if not isinstance(row, dict):
                raise OriginQuoteError("invalid bill of materials row")
            source = row.get("input")
            weight = _fraction(row.get("fraction"), "bill fraction")
            if source not in offers:
                raise OriginQuoteError("bill of materials references unknown item")
            if weight <= 0:
                raise OriginQuoteError("bill fraction must be positive")
            pairs.append((source, weight))
        if len({source for source, _ in pairs}) != len(pairs):
            raise OriginQuoteError("duplicate bill of materials input")
        if sum((weight for _, weight in pairs), Fraction(0)) != 1:
            raise OriginQuoteError("bill fractions do not sum to one")
        graph[item] = pairs
    constraints = contract.get("independent_evidence_constraints")
    if not isinstance(constraints, list):
        raise OriginQuoteError("missing independent evidence constraints")
    parsed_constraints = [_parse_constraint(raw, offers) for raw in constraints]
    return {
        "finished": finished,
        "threshold": threshold,
        "standard_rate": standard_rate,
        "preference_rate": preference_rate,
        "terminal_values": terminal_values,
        "graph": graph,
        "constraints": parsed_constraints,
    }


def _rref(rows: list[tuple[list[Fraction], Fraction]], n: int) -> tuple[list[Fraction], list[list[Fraction]]] | None:
    """Row-reduce equalities; return (particular, nullspace columns) or None if inconsistent."""
    matrix = [list(coeffs) + [rhs] for coeffs, rhs in rows]
    pivots: list[int] = []
    r = 0
    for col in range(n):
        pivot_row = next((i for i in range(r, len(matrix)) if matrix[i][col] != 0), None)
        if pivot_row is None:
            continue
        matrix[r], matrix[pivot_row] = matrix[pivot_row], matrix[r]
        head = matrix[r][col]
        matrix[r] = [value / head for value in matrix[r]]
        for i in range(len(matrix)):
            if i != r and matrix[i][col] != 0:
                factor = matrix[i][col]
                matrix[i] = [a - factor * b for a, b in zip(matrix[i], matrix[r])]
        pivots.append(col)
        r += 1
        if r == len(matrix):
            break
    for i in range(r, len(matrix)):
        if all(matrix[i][col] == 0 for col in range(n)) and matrix[i][n] != 0:
            return None
    free_cols = [col for col in range(n) if col not in set(pivots)]
    particular = [Fraction(0)] * n
    for i, col in enumerate(pivots):
        particular[col] = matrix[i][n]
    nullspace = []
    for free in free_cols:
        direction = [Fraction(0)] * n
        direction[free] = Fraction(1)
        for i, col in enumerate(pivots):
            direction[col] = -matrix[i][free]
        nullspace.append(direction)
    return particular, nullspace


def _simplex_max(c: list[Fraction], a_rows: list[list[Fraction]], b_vals: list[Fraction]):
    """Maximize c.u subject to a_rows[i].u <= b_vals[i], u >= 0, with exact rationals.

    Two-phase simplex with Bland's rule. Returns (status, value, point, duals)
    where status is "optimal", "unbounded" or "infeasible". At "optimal",
    duals[i] is the reduced cost of row i's slack: duals >= 0,
    duals^T A >= c componentwise and duals^T b == value.
    """
    m = len(a_rows)
    n = len(c)
    negated = [b_vals[i] < 0 for i in range(m)]
    art_of: dict[int, int] = {}
    ncols = n + m
    for i in range(m):
        if negated[i]:
            art_of[i] = ncols
            ncols += 1
    tableau: list[list[Fraction]] = []
    basis: list[int] = []
    for i in range(m):
        sign = Fraction(-1) if negated[i] else Fraction(1)
        row = [Fraction(0)] * (ncols + 1)
        for j in range(n):
            row[j] = sign * a_rows[i][j]
        row[n + i] = sign
        if i in art_of:
            row[art_of[i]] = Fraction(1)
            basis.append(art_of[i])
        else:
            basis.append(n + i)
        row[ncols] = sign * b_vals[i]
        tableau.append(row)
    artificial = set(art_of.values())

    def pivot(leave: int, enter: int) -> None:
        head = tableau[leave][enter]
        tableau[leave] = [value / head for value in tableau[leave]]
        for i in range(m):
            if i != leave and tableau[i][enter] != 0:
                factor = tableau[i][enter]
                tableau[i] = [a - factor * b for a, b in zip(tableau[i], tableau[leave])]

    def run(objective: list[Fraction], banned: set[int]) -> tuple[str, list[Fraction]]:
        zrow = [-value for value in objective] + [Fraction(0)]
        for i in range(m):
            if zrow[basis[i]] != 0:
                factor = zrow[basis[i]]
                zrow = [a - factor * b for a, b in zip(zrow, tableau[i])]
        while True:
            enter = next((j for j in range(ncols) if j not in banned and zrow[j] < 0), -1)
            if enter < 0:
                return "optimal", zrow
            leave = -1
            best: Fraction | None = None
            for i in range(m):
                coeff = tableau[i][enter]
                if coeff > 0:
                    ratio = tableau[i][ncols] / coeff
                    if best is None or ratio < best or (ratio == best and basis[i] < basis[leave]):
                        best = ratio
                        leave = i
            if leave < 0:
                return "unbounded", zrow
            pivot(leave, enter)
            if zrow[enter] != 0:
                factor = zrow[enter]
                zrow = [a - factor * b for a, b in zip(zrow, tableau[leave])]
            basis[leave] = enter

    if artificial:
        phase_one = [Fraction(0)] * ncols
        for col in artificial:
            phase_one[col] = Fraction(-1)
        status, _ = run(phase_one, set())
        # Phase one maximizes the negated artificial sum; below zero is infeasible.
        if status != "optimal" or sum(tableau[i][ncols] for i in range(m) if basis[i] in artificial) != 0:
            return "infeasible", None, None, None
        for i in range(m):
            if basis[i] in artificial:
                swap = next((j for j in range(ncols) if j not in artificial and tableau[i][j] != 0), -1)
                if swap >= 0:
                    pivot(i, swap)
                    basis[i] = swap
                # Otherwise the row is redundant and the artificial stays basic at zero.
    status, zrow = run(list(c) + [Fraction(0)] * (ncols - n), artificial)
    if status == "unbounded":
        return "unbounded", None, None, None
    point = [Fraction(0)] * n
    for i in range(m):
        if basis[i] < n:
            point[basis[i]] = tableau[i][ncols]
    duals = [zrow[n + i] for i in range(m)]
    return "optimal", zrow[ncols], point, duals


def _optimize(c: list[Fraction], rows: list[tuple[list[Fraction], Fraction, str]]):
    """Maximize c.t over the kept t-space rows; t is free, so it is split."""
    d = len(c)
    if not rows:
        if all(value == 0 for value in c):
            return "optimal", Fraction(0), [Fraction(0)] * d, []
        return "unbounded", None, None, None
    a_split = [list(coeffs) + [-value for value in coeffs] for coeffs, _, _ in rows]
    c_split = list(c) + [-value for value in c]
    status, value, point, duals = _simplex_max(c_split, a_split, [rhs for _, rhs, _ in rows])
    if status != "optimal":
        return status, None, None, None
    t = [point[k] - point[k + d] for k in range(d)]
    return "optimal", value, t, duals


def _assignment_ok(items, terminal_values, graph, constraints, assignment) -> bool:
    for item in items:
        value = assignment[item]
        if not Fraction(0) <= value <= Fraction(1):
            return False
        if item in terminal_values and value != terminal_values[item]:
            return False
        if item in graph and value != sum((w * assignment[src] for src, w in graph[item]), Fraction(0)):
            return False
    for terms, op, right in constraints:
        total = sum((alpha * assignment[item] for item, alpha in terms), Fraction(0))
        if (op == "<=" and total > right) or (op == ">=" and total < right) or (op == "==" and total != right):
            return False
    return True


def _certificate_ok(c, rows, duals, value) -> bool:
    if any(y < 0 for y in duals):
        return False
    for k in range(len(c)):
        if sum((y * row[0][k] for y, row in zip(duals, rows)), Fraction(0)) != c[k]:
            return False
    return sum((y * row[1] for y, row in zip(duals, rows)), Fraction(0)) == value


def _solve_bounds(items, terminal_values, graph, constraints, finished) -> dict[str, Any]:
    index = {item: i for i, item in enumerate(items)}
    n = len(items)
    equalities = []
    for item in items:
        coeffs = [Fraction(0)] * n
        coeffs[index[item]] = Fraction(1)
        if item in terminal_values:
            equalities.append((coeffs, terminal_values[item]))
        else:
            for source, weight in graph[item]:
                coeffs[index[source]] -= weight
            equalities.append((coeffs, Fraction(0)))
    eliminated = _rref(equalities, n)
    if eliminated is None:
        return {"feasibility": "infeasible"}
    particular, nullspace = eliminated
    d = len(nullspace)
    rows: list[tuple[list[Fraction], Fraction, str]] = []
    for i, item in enumerate(items):
        direction = [nullspace[k][i] for k in range(d)]
        rows.append((direction, Fraction(1) - particular[i], f"{item} share <= 1"))
        rows.append(([-value for value in direction], particular[i], f"{item} share >= 0"))
    for terms, op, right in constraints:
        coeffs = [Fraction(0)] * d
        const = Fraction(0)
        for item, alpha in terms:
            j = index[item]
            for k in range(d):
                coeffs[k] += alpha * nullspace[k][j]
            const += alpha * particular[j]
        desc = "independent: " + " + ".join(f"{alpha}*{item}" for item, alpha in terms) + f" {op} {right}"
        if op in ("<=", "=="):
            rows.append((list(coeffs), right - const, desc if op == "<=" else f"{desc} (<= half)"))
        if op in (">=", "=="):
            rows.append(([-value for value in coeffs], const - right, desc if op == ">=" else f"{desc} (>= half)"))
    kept = []
    for coeffs, rhs, desc in rows:
        if all(value == 0 for value in coeffs):
            if rhs < 0:
                return {"feasibility": "infeasible"}
        else:
            kept.append((coeffs, rhs, desc))
    fidx = index[finished]
    objective = [nullspace[k][fidx] for k in range(d)]
    if d == 0:
        share = particular[fidx]
        assignment = {item: particular[i] for i, item in enumerate(items)}
        proof = {"objective_constant": str(share), "multipliers": [], "verified": True}
        return {
            "feasibility": "unique",
            "min": share,
            "max": share,
            "min_assignment": assignment,
            "max_assignment": assignment,
            "min_proof": {**proof, "objective": "min"},
            "max_proof": {**proof, "objective": "max"},
        }
    status, vmax, tmax, ymax = _optimize(objective, kept)
    if status != "optimal":
        return {"feasibility": status}
    negated = [-value for value in objective]
    status, vneg, tmin, ymin = _optimize(negated, kept)
    if status != "optimal":
        return {"feasibility": status}
    if not _certificate_ok(objective, kept, ymax, vmax) or not _certificate_ok(negated, kept, ymin, vneg):
        raise OriginQuoteError("internal bound proof failed")
    share_min = particular[fidx] - vneg
    share_max = particular[fidx] + vmax
    unique = True
    for i in range(n):
        if all(nullspace[k][i] == 0 for k in range(d)):
            continue
        direction = [nullspace[k][i] for k in range(d)]
        _, hi, _, _ = _optimize(direction, kept)
        _, lo, _, _ = _optimize([-value for value in direction], kept)
        if hi is None or lo is None or hi != -lo:
            unique = False
            break

    def assignment(t):
        return {
            item: particular[i] + sum((t[k] * nullspace[k][i] for k in range(d)), Fraction(0))
            for i, item in enumerate(items)
        }

    min_assignment = assignment(tmin)
    max_assignment = assignment(tmax)
    for candidate in (min_assignment, max_assignment):
        if not _assignment_ok(items, terminal_values, graph, constraints, candidate):
            raise OriginQuoteError("internal assignment check failed")

    def proof(label, duals):
        return {
            "objective": label,
            "objective_constant": str(particular[fidx]),
            "multipliers": [
                {"constraint": desc, "multiplier": str(multiplier)}
                for (_, _, desc), multiplier in zip(kept, duals)
                if multiplier != 0
            ],
            "verified": True,
        }

    return {
        "feasibility": "unique" if unique else "multiple",
        "min": share_min,
        "max": share_max,
        "min_assignment": min_assignment,
        "max_assignment": max_assignment,
        "min_proof": proof("min", ymin),
        "max_proof": proof("max", ymax),
    }


def _cyclic_components(items, graph) -> list[list[str]]:
    adjacency = {item: ([source for source, _ in graph[item]] if item in graph else []) for item in items}
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = 0
    components = []
    for start in items:
        if start in index_of:
            continue
        work = [(start, 0)]
        while work:
            node, child = work[-1]
            if child == 0:
                index_of[node] = lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descend = False
            for k in range(child, len(adjacency[node])):
                nxt = adjacency[node][k]
                if nxt not in index_of:
                    work[-1] = (node, k + 1)
                    work.append((nxt, 0))
                    descend = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if descend:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1 or component[0] in adjacency[component[0]]:
                    components.append(sorted(component))
    return sorted(components)


def _cny(amount: Fraction) -> str:
    cents = floor(amount * 100 + Fraction(1, 2))
    return f"{cents // 100}.{cents % 100:02d}"


def _duty_quotes(price_cny, threshold, standard_rate, preference_rate, share_min, share_max) -> dict[str, Any]:
    price = _fraction(price_cny, "offer price")
    guaranteed = share_min >= threshold
    plausible = share_max >= threshold

    def row(contract, quantifier, eligible, rate, basis):
        return {
            "contract": contract,
            "quantifier": quantifier,
            "preference_eligible": eligible,
            "applied_rate": str(rate),
            "duty_cny": _cny(price * rate),
            "basis": basis,
            "internal_scenario_only": True,
            "publishable": False,
        }

    g_basis = (
        f"every feasible assignment reaches threshold {threshold} (minimum {share_min})"
        if guaranteed
        else f"minimum feasible share {share_min} is below threshold {threshold}; standard rate applies"
    )
    p_basis = (
        f"a feasible assignment reaches threshold {threshold} (maximum {share_max}); "
        "scenario only, origin proof pending"
        if plausible
        else f"maximum feasible share {share_max} is below threshold {threshold}; no qualifying assignment exists"
    )
    plausible_row = row(
        "plausible_origin_quote", "exists_feasible_assignment", plausible,
        preference_rate if plausible else standard_rate, p_basis,
    )
    plausible_row["pending_origin_proof"] = True
    plausible_row["customs_document"] = False
    return {
        "guaranteed_origin_quote": row(
            "guaranteed_origin_quote", "for_all_feasible_assignments", guaranteed,
            preference_rate if guaranteed else standard_rate, g_basis,
        ),
        "plausible_origin_quote": plausible_row,
    }


def quote_origin(catalog: dict[str, Any], contract_path: str | Path) -> dict[str, Any]:
    """Bind a verified catalog to an origin contract and quote both synthetic contracts."""
    offers = _validate_catalog(catalog)
    contract = _read_contract(contract_path)
    model = _validate_contract(contract, offers)
    items = sorted(offers)
    solved = _solve_bounds(items, model["terminal_values"], model["graph"], model["constraints"], model["finished"])
    feasible = solved["feasibility"] in ("unique", "multiple")
    relations = []
    for item in items:
        if item in model["terminal_values"]:
            relations.append({"item_id": item, "kind": "terminal", "share": str(model["terminal_values"][item])})
        else:
            expression = " + ".join(f"{weight}*{source}" for source, weight in model["graph"][item])
            relations.append({"item_id": item, "kind": "bill_of_materials", "expression": expression})
    quote = {
        "schema_version": 1,
        "run_id": catalog["run_id"],
        "audit_fingerprint": catalog["audit_fingerprint"],
        "catalog_fingerprint": catalog["catalog_fingerprint"],
        "contract_fingerprint": sha256_bytes(canonical_json(contract)),
        "finished_item_id": model["finished"],
        "finished_price_cny": offers[model["finished"]]["price_cny"],
        "regional_threshold": str(model["threshold"]),
        "standard_duty_rate": str(model["standard_rate"]),
        "preference_duty_rate": str(model["preference_rate"]),
        "fixed_point_relations": relations,
        "cyclic_components": _cyclic_components(items, model["graph"]),
        "feasibility": solved["feasibility"],
        "origin_share_bounds": (
            {"min": str(solved["min"]), "max": str(solved["max"])} if feasible else None
        ),
        "bound_assignments": (
            {
                "min": {item: str(value) for item, value in solved["min_assignment"].items()},
                "max": {item: str(value) for item, value in solved["max_assignment"].items()},
            }
            if feasible else None
        ),
        "bound_proofs": (
            {"min": solved["min_proof"], "max": solved["max_proof"]} if feasible else None
        ),
        "duty_quotes": (
            _duty_quotes(
                offers[model["finished"]]["price_cny"], model["threshold"],
                model["standard_rate"], model["preference_rate"], solved["min"], solved["max"],
            )
            if feasible else None
        ),
        "internal_scenario_only": True,
        "publishable": False,
    }
    quote["quote_fingerprint"] = sha256_bytes(canonical_json(quote))
    return quote


def verify_origin_quote(quote: dict[str, Any], catalog: dict[str, Any], contract_path: str | Path) -> dict[str, Any]:
    """Recompute the quote for the current catalog and contract; reject stale reuse."""
    fresh = quote_origin(catalog, contract_path)
    if not isinstance(quote, dict) or quote != fresh:
        raise OriginQuoteError("stale or foreign origin quote; recompute for the current catalog and contract")
    return fresh
