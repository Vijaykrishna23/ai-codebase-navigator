# AI Codebase Navigator — MVP Problem Statement

## Project

**AI Codebase Navigator**

## Problem

When engineers join or investigate an unfamiliar software repository, understanding how a feature works often requires manually navigating through many files, functions, classes, imports, references, and tests.

Existing AI coding tools can retrieve and explain relevant code, but the user may still need to guide the investigation through increasingly specific questions or manually navigate the repository.

The MVP aims to build an **AI Codebase Navigator** that takes a public GitHub repository and answers natural-language questions by actively investigating the codebase using code search, structural relationships, and relevant source code.

The goal is not simply to retrieve similar code, but to connect:

> **High-level explanation ↔ actual code ↔ relationships between code elements**

---

## MVP Goal

Given:

- A public GitHub repository URL
- A natural-language question

the system should determine what repository information it needs, investigate the relevant parts of the codebase, and return an **evidence-backed explanation** containing:

1. **High-level understanding** — what is happening and how the pieces fit together.
2. **Code-level evidence** — the exact files, symbols, and relevant code locations supporting the explanation.

The user should not need to know how the repository is structured or which search/analysis operations are required.

---

## Example

User provides:

`https://github.com/example/project`

Then asks:

> How does token validation work?

The system should conceptually produce:

### High-level explanation

Authentication starts in the request middleware.

The middleware extracts the token and passes it to `validate_token()`, which verifies the token and resolves the associated user. The authenticated user is then attached to the request before the request reaches the API handler.

### Code flow

```text
Request
   ↓
AuthMiddleware.authenticate()
   │
   │ calls
   ↓
validate_token()
   │
   ├── decode_token()
   ├── verify_signature()
   └── get_user()
```

### Code references

```text
middleware/auth.py
  AuthMiddleware.authenticate()       line 42

auth/token.py
  validate_token()                    line 18

auth/jwt.py
  decode_token()                      line 31

auth/user.py
  get_user()                          line 54

tests/auth/test_token.py              line 20
```

The user should be able to follow these references to the original code on GitHub.

---

# Core MVP Capabilities

## 1. Repository Ingestion

Accept a public GitHub repository URL and build a searchable representation of the repository.

The system should understand:

- Repository structure
- Directories
- Files
- Programming languages
- Source code

### Initial language support

- Python
- JavaScript
- TypeScript

---

## 2. Code Search

The system should locate relevant code using multiple retrieval mechanisms:

- Exact text search
- Identifier/symbol search
- Semantic search

For example:

> Where is token validation implemented?

should identify likely implementations even when the user's wording does not exactly match the source.

---

## 3. Basic Code Intelligence

The system should understand fundamental relationships between code elements.

Conceptually:

```text
Repository
   ↓
File
   ↓
Module
   ↓
Class / Function
   ↓
Imports / References
   ↓
Callers / Callees
```

At minimum, the MVP should support questions such as:

- Where is this function defined?
- Where is it called?
- What does it call?
- What imports this module?
- Where is this class used?
- What files are related to this code?

---

## 4. Natural-Language Investigation

The user should ask normal questions without needing to know which internal analysis operation is required.

For example:

> How does authentication work?

The system may internally perform:

```text
Search
  ↓
Find authentication entrypoint
  ↓
Identify relevant symbols
  ↓
Inspect implementation
  ↓
Find callers
  ↓
Find important callees
  ↓
Retrieve relevant source
  ↓
Construct flow
  ↓
Generate explanation
```

Whereas:

> Where is validate_token called?

may require:

```text
Find symbol
  ↓
Find references
  ↓
Group references
  ↓
Answer
```

The system determines the appropriate investigation strategy based on the question.

---

## 5. High-Level + Code-Level Answers

Substantive answers should connect high-level reasoning to concrete repository evidence.

The system should distinguish between:

### Repository facts

> `AuthMiddleware.authenticate()` calls `validate_token()`.

### AI interpretation

> This appears to centralize authentication logic so the middleware does not contain the token-validation details.

The system should avoid presenting an LLM inference as an explicitly documented fact.

When evidence is insufficient, the system should say so rather than inventing a reason.

---

## 6. Code References

Answers should provide, where available:

- Repository-relative file path
- Function/class/symbol
- Line number or relevant line range
- Link to the corresponding GitHub source

Example:

```text
src/auth/token.py
validate_token()
lines 18–43
```

---

# MVP Question Categories

## Understanding

- What does this repository do?
- Explain this module.
- What does this function do?
- What are the main components of this project?
- Explain this repository as if I'm new to it.

## Location

- Where is authentication implemented?
- Where is this API defined?
- Where is this behavior implemented?
- Which files are responsible for this feature?

## Relationships

- What calls this function?
- What does this function call?
- Where is this class used?
- What modules depend on this?
- What imports this module?

## Flow

- How does authentication work?
- How does token validation work?
- How does a request flow through the application?
- How does this feature work?
- What happens when this function is called?

## Testing

- What tests cover this functionality?
- Where are the tests for this function?
- Which tests exercise this code path?

---

# Explicitly Out of Scope for MVP

The following should not be part of the initial MVP:

- Git history analysis
- Git blame
- GitHub Issues/PR analysis
- Code generation
- Automatic code modification
- PR creation
- Autonomous bug fixing
- Full architecture visualization
- Support for many additional programming languages
- Private repositories
- IDE extensions
- Real-time repository synchronization

These may become future capabilities after the MVP proves the core concept.

---

# Core Technical Hypothesis

The project should test the following hypothesis:

> **Can combining traditional code search, structural code relationships, and LLM reasoning produce substantially more useful explanations of unfamiliar codebases than semantic RAG over source-code chunks alone?**

This makes the project an engineering experiment rather than simply another "chat with your codebase" application.

A useful eventual comparison is:

```text
Baseline
LLM + RAG
     vs
Navigator
LLM + RAG + Code Intelligence + Investigation
```

The systems can be evaluated against the same questions on real-world repositories.

---

# North-Star Definition

### One sentence

> **AI Codebase Navigator is a tool that investigates unfamiliar GitHub repositories and answers natural-language questions by connecting high-level explanations to the exact code, symbols, relationships, and tests involved.**

### Core interaction

```text
Natural-language question
          ↓
Investigation strategy
          ↓
Repository search + code intelligence
          ↓
Evidence gathering
          ↓
High-level reasoning
          +
Code-level references
          ↓
Evidence-backed answer
```

---

# Product Principle

The user should not have to think:

> "I need to search for this symbol, find its callers, follow its callees, inspect its tests, and then piece everything together."

The user should simply be able to ask:

> **"How does this work?"**

The Navigator should determine what needs to be investigated and present the result at both the **system level** and the **code level**.

---

# Future Direction — Not MVP

Once the core investigation system works, potential extensions include:

- Git commits and blame
- GitHub issues and pull requests
- More advanced code graphs
- Architecture visualization
- Change-impact analysis
- "Why was this introduced?" analysis
- Implementation planning
- Code generation
- Automated code changes
- Pull request creation

The future product direction can be:

```text
Understand
    ↓
Investigate
    ↓
Explain
    ↓
Plan
    ↓
Modify
    ↓
Test
    ↓
Create PR
```

But the MVP should remain focused on:

> **Understand → Investigate → Explain**
