# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
import json

# Market status constants
STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"


class Market(gl.Record):
    question: str
    deadline: u256       # Unix timestamp after which resolution is allowed
    creator: Address
    yes_votes: u256
    no_votes: u256
    status: str          # STATUS_OPEN or STATUS_RESOLVED
    outcome: bool        # Only meaningful when status == STATUS_RESOLVED
    outcome_set: bool    # Explicit flag — False until resolved


class PredictionMarket(gl.Contract):
    """
    A permissionless on-chain prediction market resolved by LLM consensus.

    Anyone can:
      - create_market(question, deadline)  — open a new yes/no market.
      - vote(market_id, position)          — cast YES/NO before the deadline.
      - resolve(market_id)                 — settle the outcome after the
                                             deadline via run_nondet_unsafe;
                                             leader and validator independently
                                             prompt the LLM and compare the
                                             `outcome` field.

    Invariant: once a market is RESOLVED its outcome never changes.
    """

    markets: TreeMap[u256, Market]
    market_votes: TreeMap[u256, TreeMap[Address, bool]]
    market_voted: TreeMap[u256, TreeMap[Address, bool]]  # tracks whether voted
    next_market_id: u256

    def __init__(self) -> None:
        self.next_market_id = u256(0)

    # ─── Read methods ────────────────────────────────────────────────────────

    @gl.public.view
    def get_market(self, market_id: u256) -> dict:
        """Return all details for a market."""
        if market_id >= self.next_market_id:
            raise gl.UserError("Market does not exist")
        m = self.markets[market_id]
        return {
            "market_id": int(market_id),
            "question": m.question,
            "deadline": int(m.deadline),
            "creator": str(m.creator),
            "yes_votes": int(m.yes_votes),
            "no_votes": int(m.no_votes),
            "status": m.status,
            "outcome": m.outcome if m.outcome_set else None,
        }

    @gl.public.view
    def get_vote(self, market_id: u256, account: Address) -> str:
        """Return 'YES', 'NO', or 'NONE' for a given address on a market."""
        if market_id >= self.next_market_id:
            raise gl.UserError("Market does not exist")
        voted_map = self.market_voted.get(market_id)
        if voted_map is None:
            return "NONE"
        has_voted = voted_map.get(account)
        if not has_voted:
            return "NONE"
        vote_map = self.market_votes.get(market_id)
        if vote_map is None:
            return "NONE"
        position = vote_map.get(account)
        return "YES" if position else "NO"

    @gl.public.view
    def get_market_count(self) -> u256:
        """Return the total number of markets created."""
        return self.next_market_id

    # ─── Write methods ───────────────────────────────────────────────────────

    @gl.public.write
    def create_market(self, question: str, deadline: u256) -> u256:
        """
        Create a new yes/no prediction market.

        Returns the new market_id.

        Reverts if:
          - question is empty
          - deadline is not in the future
        """
        if not question.strip():
            raise gl.UserError("Question must not be empty")

        current_time = u256(gl.message.timestamp)
        if deadline <= current_time:
            raise gl.UserError("Deadline must be in the future")

        market_id = self.next_market_id
        self.markets[market_id] = Market(
            question=question,
            deadline=deadline,
            creator=gl.message.sender_account,
            yes_votes=u256(0),
            no_votes=u256(0),
            status=STATUS_OPEN,
            outcome=False,
            outcome_set=False,
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
            raise gl.UserError("Market does not exist")

        m = self.markets[market_id]

        if m.status == STATUS_RESOLVED:
            raise gl.UserError("Market is already resolved")

        current_time = u256(gl.message.timestamp)
        if current_time >= m.deadline:
            raise gl.UserError("Voting period has ended")

        caller: Address = gl.message.sender_account

        # Check if already voted
        voted_map = self.market_voted.get(market_id, TreeMap())
        if voted_map.get(caller, False):
            raise gl.UserError("Already voted on this market")

        # Record vote
        vote_map = self.market_votes.get(market_id, TreeMap())
        vote_map[caller] = position
        self.market_votes[market_id] = vote_map

        voted_map[caller] = True
        self.market_voted[market_id] = voted_map

        # Update tally
        if position:
            m.yes_votes = m.yes_votes + u256(1)
        else:
            m.no_votes = m.no_votes + u256(1)
        self.markets[market_id] = m

    @gl.public.write
    def resolve(self, market_id: u256) -> None:
        """
        Resolve a market after its deadline using LLM consensus.

        The leader and each validator independently prompt the LLM with the
        market question and compare the `outcome` boolean field. Both must
        agree for the result to be committed.

        If consensus is not reached the transaction goes undetermined — the
        market stays OPEN/PENDING and resolve can be retried.

        Reverts if:
          - market does not exist
          - deadline has not yet passed
          - market is already resolved
        """
        if market_id >= self.next_market_id:
            raise gl.UserError("Market does not exist")

        m = self.markets[market_id]

        if m.status == STATUS_RESOLVED:
            raise gl.UserError("Market is already resolved")

        current_time = u256(gl.message.timestamp)
        if current_time < m.deadline:
            raise gl.UserError("Deadline has not passed yet")

        question = m.question

        prompt = f"""You are an impartial prediction market resolver.
A prediction market asked the following yes/no question:

"{question}"

Using your knowledge and reasoning, determine whether the answer to this
question is YES (true) or NO (false) as of today.

Consider publicly available information, historical data, and logical reasoning.
If the question refers to a future event that is genuinely unknowable at this
point in time, answer false (NO) as a conservative default.

Respond with ONLY valid JSON in exactly this format, nothing else:
{{
  "outcome": true,
  "reasoning": "one sentence explanation"
}}
or
{{
  "outcome": false,
  "reasoning": "one sentence explanation"
}}
"""

        def _call_llm() -> dict:
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
            return _call_llm()

        def validator_fn(leader_result: gl.vm.Result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False

            leader_outcome: bool = leader_result.calldata.get("outcome", False)

            try:
                validator_result = _call_llm()
            except Exception:
                return False

            validator_outcome: bool = validator_result.get("outcome", False)

            # Both must independently agree on the outcome
            return leader_outcome == validator_outcome

        consensus_result: dict = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        outcome: bool = consensus_result.get("outcome", False)

        m.status = STATUS_RESOLVED
        m.outcome = outcome
        m.outcome_set = True
        self.markets[market_id] = m
