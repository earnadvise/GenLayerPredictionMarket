# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
from dataclasses import dataclass
import json

# Market status constants
STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"

# Search URL template — uses DuckDuckGo HTML search so no API key is needed
# and every validator can independently re-fetch the same query.
SEARCH_URL_TEMPLATE = "https://html.duckduckgo.com/html/?q={query}"


@allow_storage
@dataclass
class Market:
    question: str
    deadline: u256       # Unix timestamp after which resolution is allowed
    creator: Address
    yes_votes: u256
    no_votes: u256
    status: str          # STATUS_OPEN or STATUS_RESOLVED
    outcome: bool        # Final outcome — only meaningful when status == STATUS_RESOLVED
    outcome_set: bool    # Explicit flag so we can distinguish False outcome from unset
    evidence_url: str    # URL that was fetched for resolution evidence


class PredictionMarket(gl.Contract):
    """
    A permissionless on-chain prediction market resolved by LLM + web evidence consensus.

    Anyone can:
      - create_market(question, deadline, evidence_url)
            — open a new yes/no market with an explicit evidence URL.
      - vote(market_id, position)
            — cast YES (True) / NO (False) before the deadline.
      - resolve(market_id)
            — after deadline, each node independently fetches the evidence URL,
              passes the live content to the LLM, and compares the `outcome`
              field via run_nondet_unsafe. Both leader and validator must agree.

    Invariant: once RESOLVED, a market's outcome never changes.
    """

    markets: TreeMap[u256, Market]
    votes: TreeMap[str, bool]       # "marketId:address" -> position (bool)
    has_voted: TreeMap[str, bool]   # "marketId:address" -> voted flag
    next_market_id: u256

    def __init__(self) -> None:
        self.next_market_id = u256(0)

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _vote_key(self, market_id: u256, account: Address) -> str:
        return f"{int(market_id)}:{account.as_hex}"

    # ─── Read methods ────────────────────────────────────────────────────────

    @gl.public.view
    def get_market(self, market_id: u256) -> dict:
        """Return all details for a market as a plain dict."""
        if market_id >= self.next_market_id:
            raise Exception("Market does not exist")
        m = self.markets[market_id]
        return {
            "market_id": int(market_id),
            "question": m.question,
            "deadline": int(m.deadline),
            "creator": m.creator.as_hex,
            "yes_votes": int(m.yes_votes),
            "no_votes": int(m.no_votes),
            "status": m.status,
            "outcome": m.outcome if m.outcome_set else None,
            "evidence_url": m.evidence_url,
        }

    @gl.public.view
    def get_vote(self, market_id: u256, account_address: str) -> str:
        """Return 'YES', 'NO', or 'NONE' for a given address on a market."""
        account = Address(account_address)
        key = self._vote_key(market_id, account)
        if not self.has_voted.get(key, False):
            return "NONE"
        return "YES" if self.votes.get(key, False) else "NO"

    @gl.public.view
    def get_market_count(self) -> u256:
        """Return the total number of markets created."""
        return self.next_market_id

    # ─── Write methods ───────────────────────────────────────────────────────

    @gl.public.write
    def create_market(self, question: str, deadline: u256, evidence_url: str) -> u256:
        """
        Create a new yes/no prediction market.

        `evidence_url` is the authoritative page validators will fetch during
        resolution (e.g. a Reuters article, an official results page, a
        Wikipedia article, or a CoinGecko price page).

        Returns the new market_id.

        Reverts if:
          - question is empty
          - evidence_url is empty
          - deadline is not in the future
        """
        if not question.strip():
            raise Exception("Question must not be empty")
        if not evidence_url.strip():
            raise Exception("evidence_url must not be empty")

        current_time = u256(gl.message.timestamp)
        if deadline <= current_time:
            raise Exception("Deadline must be in the future")

        market_id = self.next_market_id
        self.markets[market_id] = Market(
            question=question,
            deadline=deadline,
            creator=gl.message.sender_address,
            yes_votes=u256(0),
            no_votes=u256(0),
            status=STATUS_OPEN,
            outcome=False,
            outcome_set=False,
            evidence_url=evidence_url,
        )
        self.next_market_id = self.next_market_id + u256(1)
        return market_id

    @gl.public.write
    def vote(self, market_id: u256, position: bool) -> None:
        """
        Cast a YES (True) or NO (False) vote on an open market.

        Reverts if:
          - market does not exist
          - deadline has already passed
          - caller has already voted on this market
          - market is already resolved
        """
        if market_id >= self.next_market_id:
            raise Exception("Market does not exist")

        m = self.markets[market_id]

        if m.status == STATUS_RESOLVED:
            raise Exception("Market is already resolved")

        current_time = u256(gl.message.timestamp)
        if current_time >= m.deadline:
            raise Exception("Voting period has ended")

        caller: Address = gl.message.sender_address
        key = self._vote_key(market_id, caller)

        if self.has_voted.get(key, False):
            raise Exception("Already voted on this market")

        self.votes[key] = position
        self.has_voted[key] = True

        if position:
            m.yes_votes = m.yes_votes + u256(1)
        else:
            m.no_votes = m.no_votes + u256(1)
        self.markets[market_id] = m

    @gl.public.write
    def resolve(self, market_id: u256) -> None:
        """
        Resolve a market after its deadline using live web evidence + LLM consensus.

        Each node (leader and every validator) independently:
          1. Fetches the market's evidence_url to get current, authoritative content.
          2. Passes that content to the LLM together with the question.
          3. Returns {outcome: bool, reasoning: str}.

        The leader proposes its result. Each validator independently fetches the
        same URL and judges the question from the live content. Both must agree
        on the `outcome` field before the result is committed on-chain.

        Because the evidence is fetched live inside run_nondet_unsafe, the LLM
        cannot rely solely on prior knowledge — it must reason from the fetched
        page content. This satisfies the independent-verification requirement.

        Reverts if:
          - market does not exist
          - deadline has not yet passed
          - market is already resolved
        """
        if market_id >= self.next_market_id:
            raise Exception("Market does not exist")

        m = self.markets[market_id]

        if m.status == STATUS_RESOLVED:
            raise Exception("Market is already resolved")

        current_time = u256(gl.message.timestamp)
        if current_time < m.deadline:
            raise Exception("Deadline has not passed yet")

        question = m.question
        evidence_url = m.evidence_url

        def _fetch_and_judge() -> dict:
            """
            Fetch live evidence and ask the LLM to judge the question from it.
            Called independently on leader and each validator node.
            """
            # Fetch the evidence page as plain text (avoids JS-heavy pages)
            page_content = gl.nondet.web.get(evidence_url, mode="text")

            # Truncate to keep the prompt manageable
            snippet = page_content[:4000] if len(page_content) > 4000 else page_content

            prompt = f"""You are an impartial prediction market resolver.

A prediction market asked the following yes/no question:
"{question}"

Below is LIVE evidence fetched from: {evidence_url}
--- EVIDENCE START ---
{snippet}
--- EVIDENCE END ---

Based ONLY on the evidence above, determine whether the answer to the question
is YES (true) or NO (false).

Rules:
- Do NOT rely on your prior knowledge. Base your answer solely on the evidence.
- If the evidence does not clearly support YES, answer false (NO).
- Be concise in your reasoning.

Respond with ONLY valid JSON in exactly this format, nothing else:
{{
  "outcome": true,
  "reasoning": "one sentence explanation referencing the evidence"
}}
or
{{
  "outcome": false,
  "reasoning": "one sentence explanation referencing the evidence"
}}
"""
            raw = gl.nondet.exec_prompt(prompt)
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
            result = json.loads(cleaned.strip())
            assert isinstance(result.get("outcome"), bool), "outcome must be bool"
            return result

        def leader_fn() -> dict:
            return _fetch_and_judge()

        def validator_fn(leader_result: gl.vm.Result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            leader_outcome: bool = leader_result.calldata.get("outcome", False)
            try:
                # Validator independently fetches and judges — does NOT trust leader
                validator_result = _fetch_and_judge()
            except Exception:
                return False
            validator_outcome: bool = validator_result.get("outcome", False)
            # Both independent verdicts must agree
            return leader_outcome == validator_outcome

        consensus_result: dict = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        outcome: bool = consensus_result.get("outcome", False)

        m.status = STATUS_RESOLVED
        m.outcome = outcome
        m.outcome_set = True
        self.markets[market_id] = m
