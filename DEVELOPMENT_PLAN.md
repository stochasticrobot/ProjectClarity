# ProjectClarity — Development Plan

This is the current development plan. It replaces the earlier plan in full; requirements and technology choices from that plan do not carry over unless explicitly reintroduced.

## Purpose

Sharpen practical skills for AI engineering jobs, specifically agentic AI engineering, LLM evaluation and observability, and AI/LLMOps/cloud.

## Service

Build a service that researches a topic from supplied sources, extracts facts, records supporting evidence, and produces a cited report.

## Weeks 1–2: Build the agent

LangGraph, source retrieval, structured output, tool calls, persistent state, retries, and human approval for consequential actions.

## Weeks 3–4: Build its evaluation system

Create roughly 100 test cases. Measure retrieval quality, factual correctness, citation accuracy, task completion, and failure categories. Compare against a simple baseline.

## Weeks 5–6: Make it production-like

Containerize it, expose a FastAPI service, deploy on AWS, add CI/CD, PostgreSQL, logging, tracing, and cost/latency dashboards.

## Weeks 7–8: Attack it, improve it, document it

Create a prompt-injection test suite. Implement authorization and tool-use protections. Quantify the improvements, publish a GitHub repository, and write a two-page technical case study.
