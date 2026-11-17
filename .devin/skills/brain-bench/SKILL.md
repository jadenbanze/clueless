---
name: brain-bench
description: Benchmark Clueless answer grounding, retrieval latency, prompt budget, and summary formatting across profiles
allowed-tools:
  - read
  - grep
  - glob
  - exec
---

Run the Clueless brain and retrieval regression benchmarks whenever prompts, profiles, RAG scoring, or provider routing change:

1. Execute the offline fast suite first:
   - `python3 -m server.test_profiles`
   - `python3 -m server.test_corrections`
   - `python3 -m server.test_textclean`
   - `python3 -m server.test_providers`
2. If a local model provider (Ollama, llama.cpp, LM Studio, or Apple Foundation Models) is running, execute the live grounding suite:
   - `python3 -m server.test_brain`
3. Evaluate and report:
   - Grounding pass/fail rate (must never invent ticket IDs, citations, dosages, or leak prompt rules)
   - Prompt token / character size against `TRANSCRIPT_MAX_CHARS` and `CONTEXT_MAX_TOTAL_CHARS`
   - Retrieval latency (target <50ms warm / <120ms cold embed) and average answer latency
   - Profile-specific summary structure (`developer`, `student`, `student` clinical mode, `general`)
