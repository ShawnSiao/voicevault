# VoiceVault `.agent` workspace

This directory contains project-specific context for AI agents. It is intended to make future work faster and safer without mixing agent notes into runtime code.

## Contents

- `instructions/`: Stable repository instructions and workflow notes.
- `skills/`: Reusable project-level skills. Each skill has a `SKILL.md` file with trigger guidance and a focused workflow.

## Usage

Agents should start with the root `AGENTS.md`, then load only the instruction or skill files that match the current task. Keep this directory free of secrets, local runtime state, imported content, generated indexes, and screenshots.
