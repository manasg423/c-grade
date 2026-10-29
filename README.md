Here is a highly detailed, deep-dive presentation script. This version expands significantly on the internal mechanics of each file, providing you with the technical depth needed to impress an evaluator and answer complex architectural questions.

---

# Comprehensive Presentation Script: C-Grade Engine (Week 2)

**"Good morning/afternoon. Today, I will be presenting the comprehensive end-to-end architecture of the C-Grade Engine.**

Over the past week, we have successfully developed, integrated, and battle-tested all 9 of our specialist analyzers, our secure execution sandbox, and the dual-portal Streamlit frontend. Our primary achievement is that the system is fully functional, producing 100% reproducible, deterministic grades without relying on unstable external APIs or fragile wall-clock timers.

I will walk you through the exact lifecycle of a student's submission, detailing the underlying engineering of our pipeline."

---

### 1. Phase 1: Pre-flight & The Secure Sandbox (`first_pass.py` & `sandbox.py`)

"When a student submits their code via the UI, it doesn't just run blindly. It hits our Gatekeeper.

First, the system validates the assignment's `rules.json`. To prevent floating-point drift, it computes a SHA-256 hash of the rules file and ensures the evaluation metrics sum to exactly 1.00.

Next, it performs a **Static Sanity Check**. It scans the C code to ensure the fundamental requirements are present: a `main()` function, standard I/O calls (`scanf`/`printf`), and a `return` statement.

If it passes, we compile it and initiate a **Smoke Test** using our `sandbox.py` module. The sandbox relies on Python’s POSIX `resource` limits (`RLIMIT_AS`). We strictly cap the memory usage at 64MB and enforce a 1-second timeout. If a student writes an infinite loop or a memory-hogging array, the OS intercepts it, the process is safely killed, and the Gatekeeper aborts the pipeline, flagging it as a 'Suspicious Loop'."

---

### 2. Phase 2: Dynamic Execution & Profiling

"If the binary is safe, it moves to our heavy dynamic analyzers.

**1. Intelligent I/O Generation (`io_tests.py`)**
We aren't just relying on static test files. Our `io_tests.py` script leverages our local Ollama AI model (`qwen3.5:2b`). We feed the Teacher's reference `oracle.c` into the LLM and ask it to deduce the expected standard-input protocol.
The LLM generates formatted, valid JSON input cases which are piped into the compiled Oracle binary. The engine records the Oracle's standard output, saves these pairs locally as `.in` and `.out` files in an `auto_generated/` cache directory, and then evaluates the student's binary against them. We enforce a strict process-group termination protocol to ensure no orphan processes are left behind during testing.

**2. Precision Asymptotic Profiling (`complexity_estimator.py`)**
This is one of our most advanced modules. Wall-clock timers fail because OS background noise drowns out microsecond C executions.
Instead, we compile the student's code using **Clang's SanitizerCoverage** (`-fsanitize-coverage=trace-pc-guard`). We inject custom C hooks that count the exact number of hardware-level execution edges the program hits. We also reroute standard `strlen` calls to a custom instrumented version to ensure hidden loops are counted.
By feeding the program mathematically scaled inputs (e.g., arrays of size 10, 30, 50, 70, 90) and tracking the exact edge-execution steps, we apply a Least Squares regression to curve-fit the data. This allows us to mathematically prove if the student wrote an $O(1)$, $O(N)$, $O(N \log N)$, or $O(N^2)$ algorithm."

---

### 3. Phase 3: Static Analysis & Academic Integrity

"While the dynamic analyzers check *what* the code does, our static analyzers check *how* it was written.

**3. Modularity & Best Practices (`static_analysis.py`, `compiler_warnings.py`, `function_split.py`)**

* We run `gcc -fsyntax-only` with strict flags (`-Wall -Wextra -Wpedantic`) to intercept and penalize sloppy warnings.
* We use `cppcheck` to catch portability issues and `lizard` to measure Cyclomatic Complexity, penalizing functions that score higher than 8.
* Instead of requiring a heavy Abstract Syntax Tree parser, our `function_split.py` uses a highly robust, custom regex engine that strips C-comments and strings, and safely identifies whether the student modularized their code with helper functions or just crammed everything monolithically into `main()`.

**4. K-Gram Plagiarism Detection (`similarity_flag.py`)**
To prevent cheating, we built a token-winnowing engine. It takes the student's code and strips all comments, whitespace, and renames all variables to generic IDs (structural normalization). It then breaks the code into overlapping 'k-grams' (chunks of 10 characters), hashes them using MD5, and selects the minimum hash in a rolling window.
By comparing these structural fingerprints against the entire corpus of previous submissions using Jaccard Similarity mathematics, we can instantly flag obfuscated, copy-pasted code if it crosses our 0.85 threshold."

---

### 4. Phase 4: AI Readability Review (`llm_review.py`)

"We also wanted a qualitative review of the student's styling. We pass the source code to our local `qwen3.5:2b` LLM, strictly locking the `temperature` to 0 and the `seed` to 42 for absolute determinism. It evaluates naming conventions and comments, returning a structured JSON score.
Crucially, if the LLM server is ever unreachable, we built a deterministic **heuristic fallback**. The fallback parses the code, calculates the ratio of indented lines, checks for the presence of block comments, and penalizes single-letter variable names, ensuring the pipeline never breaks."

---

### 5. Agile Testing Architecture & UI Orchestration

"All of this complex backend logic is orchestrated beautifully by our Streamlit UI (`ui.py`). The UI dynamically parses the Gatekeeper's nested dictionaries and uses `importlib.reload` to ensure it always runs the freshest analyzer scripts from the disk without caching issues.

To ensure the reliability, accuracy, and robustness of the C-Grade evaluator, comprehensive testing protocols were established. **We are making use of an Agile iterative methodology in our testing and observing loops.**

While we use `test_tool.py` via the command line to independently test every analyzer in isolation (simulating inputs and checking raw JSON outputs), **we use our developed Streamlit UI to test the project end-to-end. To test end-to-end, we are displaying the output of every analyzer on the UI, so that we can observe and understand the issues if there are any. The test cases are also displayed on the UI - so that we can efficiently debug and fix the issues that arise, thus improving our project.**"

---

### 6. Conclusion & Week 3 Outlook

"To summarize, we have a fully functional, highly advanced, deterministic grading engine. The system currently executes these 9 analyzers sequentially to guarantee precision.

Our final goal for Week 3 is to wrap these isolated analyzers into a `ThreadPoolExecutor`. This will allow us to run all 9 analyzers in parallel, dramatically cutting down the grading time per student while ensuring thread-safe file generation.

Thank you. I would now be happy to answer any technical questions about the implementation."