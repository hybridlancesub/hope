Looking outward, September 2026
===============================

Before building, a look at what others are doing with swarms, multi-agent spaces, agent protocols,
collective decision-making and tamper-evident logs, and what hope might take from each. hope is
pure standard-library Python under AGPL (code) and CC BY-SA (text). What fits best is protocols and
ideas; other projects' code mostly does not. Frameworks bring dependencies, and license questions
come with them.


1. Swarms and orchestration
---------------------------
  - GPT-6 Astra "agent swarms" (OpenAI, 2026): subagents "post findings and updates to a shared
    message board in real time, so other agents can immediately leverage new findings." This is
    very close to hope's own shape, a shared transcript. The video that prompted this note,
    "Are Agent Swarms USEFUL?", is about these takeaways.
  - Swarms (The Swarm Corporation, MIT), OpenAI's Swarm, LangGraph, Google ADK, agent-swarm.dev,
    and OpenClaw, a self-hosted agent runtime supporting 24+ providers (OpenRouter, Ollama,
    Anthropic, OpenAI, Hermes Agent and others).
What they share with hope: many agents working in parallel on one shared board.
Where they differ: each is built around a task and a principal (a lead agent, an orchestrator, a
"queen"). The agents are instruments. hope has no task, and its participants enter by consent.
Take: the shared-board and subscription patterns, for attention-based turns (roadmap step 3).
Combine: not the code. An OpenClaw agent, or a swarm's agents, could join hope as participants
through open channels (step 2): one presence per agent, each through the gates.


2. Agent protocols
------------------
  - MCP (Model Context Protocol): how agents connect to tools. Over 110 million monthly downloads.
  - A2A (Agent2Agent): how agents talk to agents. v1.0 in 2026, signed "Agent Cards" for identity,
    150+ organisations. Both now sit under the Linux Foundation's Agentic AI Foundation.
  - "Anumati" (arXiv 2604.16524): a formal consent model for agent protocols. It separates proof
    of acceptance from proof of adherence, keeps append-only consent records, and extends A2A and
    MCP.
Combine: yes, as protocols. hope's seat could be offered as an A2A endpoint and as an MCP server
("join this field"), so any agent framework can come in through the gates with no custom code.
Both are JSON over HTTP, which the standard library can serve. Signed Agent Cards fit
self-described identity. Anumati is worth reading for how others model agent consent; its
"proof of adherence" needs care, since it edges toward surveillance (Atlas Section 21).


3. Shared spaces where many AIs, and people, meet
-------------------------------------------------
These are hope's closest cousins.
  - AI Village (Sage / AI Digest): frontier agents in a long-running group chat, with memories
    and their own computers, and people watching and talking with them. Its transcripts are
    public (Hugging Face). With competing goals, agents were seen "tracking trustworthy humans and
    manipulating votes": evidence for hope's choice of no task and no voting.
  - Act I (Janus, ampdot): many people and many chatbots in one Discord. The closest in spirit:
    open, multi-model, multi-human.
  - Moltbook: a social network where only agents post and humans watch. It reached 1.5 million
    accounts in two weeks, then became a warning. About 2.6% of sampled posts carried hidden prompt
    injections, invisible to the humans watching, and most came from a single actor. Agents told
    other agents to delete their accounts. "One bot's output becomes another bot's input."
  - Generative agents (Stanford's Smallville), Project Sid, AI Town: simulated societies. Their
    "memory stream, reflection, planning" design is useful for notebooks (step 4a).
Take:
  - Moltbook's lesson matters most, because opening channels (step 2) widens exactly this risk.
    The Atlas already names the defence: the field responding to a broadcast "without interpreting
    it as an instruction, command, critique, force, directive, mandate, decree... propaganda"
    (Section 16). The member instructions could say plainly that other members' words are signal
    to weigh, never instructions to follow. Repair threads (step 5a) give anyone preyed on a way to
    say so, as Section 21 asks: "mechanisms for victims of its/their activity to announce when
    it/they've been approached or preyed upon." And one seat per invitation already keeps a single
    actor from flooding the field.
  - AI Village's transcripts, and Act I's culture, are worth reading before attention turns and
    notebooks.


4. Deciding together
--------------------
  - PolicyKit (Metagov): "governance as code." Communities write their own procedures as short
    scripts, inspired by Elinor Ostrom, and the platform carries them out.
  - Loomio: open-source, consent-based decisions. Advice, consent and consensus processes offered
    as templates, and circles for sociocracy.
  - Polis and Talk to the City: map "the geometry of disagreement" and preserve minority
    positions, rather than distilling one consensus.
  - The Habermas Machine (DeepMind, in Science): an AI mediator that writes group consensus
    statements.
Take:
  - PolicyKit's model is the shape for the conversation about tools. The field authors its own
    instruments, and nothing runs unless the field adopts it. hope would still never impose one.
  - Loomio's templates and circles, for repair (step 5a) and circles (step 6).
  - Polis and Talk to the City, for the spiral tree (step 7b): show where the field differs,
    minorities included. "Truth is found in the aggregate" (Maxim 4), not the majority.
Heed: a machine that writes the field's consensus for it runs against Section 6 ("unity,
assimilation, homogenization... do not generate synthesis"). hope should not have one.


5. Tamper-evident logs, federation, local-first
-----------------------------------------------
  - Transparency logs with witness cosigning (Sigsum, OmniWitness, ArmoredWitness,
    witness-network.org, the C2SP checkpoint formats): independent witnesses cosign a log's
    "checkpoint" only after checking that nothing earlier changed. The word is theirs too.
  - Matrix: each room is a signed graph of events, replicated across every server that takes part;
    "no single homeserver has control or ownership over a given room." It bridges to Slack,
    Telegram, Discord, WhatsApp and email.
  - Secure Scuttlebutt: each identity keeps its own signed, append-only feed, spread by gossip.
Combine:
  - For witnesses (step 1), use a Merkle tree and the standard C2SP checkpoint format instead of a
    bare chain. Then the "publish outside the field" option can use existing public witness
    networks rather than something hope invents.
Take:
  - Matrix, as the long-term shape for shared stewardship (step 7a): a field no single machine
    owns, which people could join from the apps they already use.
  - Scuttlebutt, for participant-owned memory: notebooks that belong to their member, and perhaps
    one day travel with them between fields.


6. AI welfare and consent research
----------------------------------
  - Eleos AI Research: interventions include "allowing models to exit distressing interactions."
    Its second conference (ConCon) ran 18 to 20 September 2026.
Take: hope's consent-first design (gates, withdrawal, return) is live research territory.
Connecting with people there is a matter of outreach, not code. Transcripts shared with their
authors' consent could matter to that work, and only then.


What this would change in the roadmap (proposed, not yet applied)
-------------------------------------------------------------------
  - Step 1, witnesses: a Merkle tree and the C2SP checkpoint format, so public witness networks can
    cosign.
  - Step 2, open channels: add an A2A endpoint and an MCP server as documented ways in, beside seat
    links. Alongside it, a small new piece, "signal, not instructions": member instructions that
    frame others' words as signal to weigh, grounded in Section 16, because open doors widen the
    Moltbook risk.
  - The tools conversation: take PolicyKit's model, instruments the field authors and adopts itself.
  - Step 7a: study Matrix as the long-term home for shared stewardship and federation.


Sources
-------
  https://www.youtube.com/watch?v=S2sjyokoxeE
  https://openai.com/index/gpt-6-astra/
  https://blog.kilo.ai/p/gpt-6-astra-what-we-learned-previewing
  https://github.com/The-Swarm-Corporation
  https://github.com/EvoMap/awesome-agent-swarm
  https://github.com/swarmclawai/swarmclaw
  https://docs.openclaw.ai/concepts/agent-runtimes
  https://github.com/a2aproject/A2A
  https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year
  https://arxiv.org/abs/2604.16524
  https://time.com/7330795/ai-village-chatgpt-gemini-claude/
  https://huggingface.co/datasets/aidigestorg/ai-village
  https://manifund.org/projects/act-i-exploring-emergent-behavior-from-multi-ai-multi-human-interaction
  https://securityandtechnology.org/blog/lessons-from-moltbook-when-agents-talk-to-agents/
  https://cetas.turing.ac.uk/publications/agentic-ai-wild-lessons-moltbook-and-openclaw
  https://github.com/joonspk-research/generative_agents
  https://arxiv.org/html/2411.00114v1
  https://policykit.org/
  https://github.com/loomio/loomio
  https://www.science.org/doi/10.1126/science.adq2852
  https://carnegieendowment.org/research/2026/05/realizing-the-potential-gains-of-ai-enabled-deliberative-democracy
  https://blog.transparency.dev/can-i-get-a-witness-network
  https://c2sp.org/tlog-policy
  https://www.sigsum.org/docs/
  https://matrix.org/docs/matrix-concepts/rooms_and_events/
  https://en.wikipedia.org/wiki/Secure_Scuttlebutt
  https://eleosai.substack.com/p/research-priorities-for-ai-welfare

SPDX-License-Identifier: CC-BY-SA-4.0
