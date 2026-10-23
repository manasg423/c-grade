# c-grade
C program auto grader agent - computing lab 1 project
we are making a project on auto grading assignment

Good morning. Today, I will walk you through the precise technical workflow of the C-Grade engine, focusing on our Week 1 deliverables: the Phase 1 Gatekeeper and our first two dynamic analyzers. I will explain exactly what happens under the hood from the moment a student hits "Submit" to the final generation of their score.

1. The Submission Phase and Initialization
The workflow begins in our Streamlit-based Student Portal. When a student uploads or pastes their C source code for a specific assignment, such as the prime number check, the UI immediately saves this file into a dedicated, sandboxed directory: submissions/prime/.

Simultaneously, the system loads the teacher-defined rules.json for that specific problem. Before any code is executed, the engine runs a fast validation check on this JSON file to guarantee that the grading metric weights mathematically sum to exactly 1.00. If a teacher accidentally configured weights that sum to 1.10, the system automatically normalizes them proportionally.

2. Phase 1: The Gatekeeper (first_pass.py)
Once the file is saved and rules are validated, the code enters Phase 1, which we call the Gatekeeper. This is a critical security and sanity layer designed to protect the host machine and prevent broken code from wasting computational resources.

The Gatekeeper executes a strict, three-step pipeline:

Step A: Static Security Scan (Banned Keywords)
Before invoking the compiler, we perform a regex-based text scan on the raw .c file. We are looking for banned keywords and malicious system calls, such as system(), fork(), exec(), or attempts to include <unistd.h> for unauthorized file I/O. If any of these are detected, the submission is immediately flagged, and the pipeline is aborted to protect our offline Ubuntu environment.

Step B: Compilation Check
If the security scan passes, we attempt to compile the code. The system uses Python's subprocess.run to invoke the GNU C Compiler with the command gcc -O0 submission.c -o submission.out -lm. We deliberately compile with -O0 (zero optimization) to ensure that the execution profiles we measure later reflect the student's actual algorithmic structure, rather than the compiler's optimizations. If the return code is non-zero, we capture the exact compilation errors from stderr, display them to the student, and completely halt the pipeline. Dynamic analyzers are skipped because there is no binary to run.

Step C: The Smoke Test
If compilation succeeds, we have an executable binary, but we still don't know if it is safe to run through our heavy analyzers. The binary might contain an infinite loop or a segmentation fault on startup. To test this, we run a "Smoke Test."
The Gatekeeper executes the binary using a safe, predefined sample input (like "10" or "hello") with a very strict, low timeout constraint—typically 0.5 to 1.0 seconds.
If the process completes within the timeout and returns an exit code of 0, it passes the smoke test. If it times out, our Python subprocess forcefully kills the C program and flags it as a "Suspicious Loop/Crash." This ensures that when we pass the binary to our 9 specialist analyzers, they won't hang indefinitely.

3. Analyzer 1: Black-Box Correctness (io_tests.py)
With a safe, compiled binary in hand, the workflow moves to Phase 2: Dynamic Analysis. The first analyzer is io_tests.py, which is responsible for high-volume correctness verification.

Instead of requiring teachers to manually write dozens of test cases, this analyzer generates them dynamically:

Input Detection: It parses the Teacher's reference solution (oracle.c). By analyzing the scanf, fgets, or getchar calls, it dynamically detects the required input format (e.g., recognizing %s means it needs string generation, while a for loop with scanf("%d") means it needs an integer array).

Oracle Execution: It compiles the Teacher's oracle.c. It then generates random inputs based on the detected format and pipes them into the Teacher's binary to capture the definitive "Expected Output."

File System Auditing: It writes these auto-generated inputs and expected outputs to the disk inside tests/<problem_id>/auto_generated/ (e.g., gen_1.in, gen_1.out). This ensures transparency, allowing the evaluator or the student to see the exact test data used.

Evaluation: Finally, it runs the Student's binary against these .in files, strictly comparing their output to the .out files. It calculates the percentage of passed tests and logs the exact inputs that caused any failures.

4. Analyzer 2: Adversarial Boundary Testing (edge_case_runner.py)
While io_tests.py checks standard functionality, our second analyzer, edge_case_runner.py, tests for structural robustness.

Students frequently write code that works for standard inputs but crashes on boundaries. This analyzer systematically injects adversarial inputs tailored to the data type.

If the problem requires integers, it injects 0, -1, and massive boundary integers.

If the problem requires strings, it injects empty strings, strings containing only spaces, or special ASCII characters.

Just like the first analyzer, it feeds these boundary cases into the Teacher's oracle.c to establish the ground truth. If the Teacher's code handles an empty string by printing Error or 0, the student's code is strictly evaluated against that exact behavior. This simulates a rigorous, real-world QA environment.

At the end of this pipeline, the Streamlit UI aggregates the results from the Gatekeeper and both analyzers, applies the mathematically balanced weights from the JSON rubric, and presents the student with a final grade and a detailed evidence log.

7335 8948
9310 0927