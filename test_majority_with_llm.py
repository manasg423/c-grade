import urllib.request
import json
import time

oracle_code = """#include <stdio.h>
#include <stdlib.h>

int find_majority(int *arr, int n) {
    if (n <= 0) return -1;
    
    // Boyer-Moore Voting Algorithm O(N)
    int candidate = arr[0];
    int count = 1;
    
    for (int i = 1; i < n; i++) {
        if (count == 0) {
            candidate = arr[i];
            count = 1;
        } else if (arr[i] == candidate) {
            count++;
        } else {
            count--;
        }
    }
    
    // In this problem, assume a majority always exists.
    return candidate;
}

int main() {
    int n;
    // Edge case: Empty input or non-integer
    if (scanf("%d", &n) != 1 || n <= 0) {
        printf("-1\\n");
        return 0;
    }
    
    int *arr = (int*)malloc(n * sizeof(int));
    if (!arr) return 1;

    for (int i = 0; i < n; i++) {
        scanf("%d", &arr[i]);
    }

    printf("%d\\n", find_majority(arr, n));

    free(arr);
    return 0;
}"""

system_prompt = (
    "You are an expert universal C programming test suite designer. "
    "Your task is to analyze ANY given C reference implementation (oracle.c) and synthesize meaningful, high-quality test cases.\n"
    "Steps:\n"
    "1. Read main() and all input-reading calls (scanf, fgets, getchar) to deduce the EXACT stdin structure: line count, formatting, and types.\n"
    "2. Understand the algorithm and business logic to ensure EVERY test case is meaningful and conforms to problem assumptions "
    "(e.g., if finding a majority element, the majority value must appear strictly > n/2 times in the array).\n"
    "3. Synthesize two lists:\n"
    "   - 'io_tests': typical, diverse, non-trivial test inputs representing valid problem instances.\n"
    "   - 'edge_cases': boundary, corner, adversarial test inputs (e.g. n=1, all identical elements, negative numbers, boundary limits, invalid inputs n<=0 if handled).\n"
    "4. Return STRICTLY a valid JSON object matching this schema:\n"
    "{\n"
    '  "io_tests": ["raw_stdin_string_1", "raw_stdin_string_2", ...],\n'
    '  "edge_cases": ["raw_stdin_string_1", "raw_stdin_string_2", ...]\n'
    "}"
)

user_prompt = (
    "Reference Implementation (oracle.c):\n"
    f"```c\n{oracle_code}\n```\n\n"
    "Generate 5 diverse 'io_tests' and 5 'edge_cases'.\n"
    "CRITICAL RULES:\n"
    "- Every string in both lists must be the EXACT raw text to pipe into stdin, ending with newline '\\n'.\n"
    "- Make sure standard inputs match the scanf contract: line 1 has n, line 2 has n space-separated integers.\n"
    "- Make sure io_tests are meaningful: in each array of size n, one number must appear > n/2 times."
)

payload = {
    "model": "qwen3.5:2b",
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ],
    "think": False,
    "format": "json",
    "stream": False,
    "options": {
        "temperature": 0.2,
        "num_predict": 1024
    }
}

t0 = time.time()
req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode("utf-8"))
    print(f"Time: {time.time()-t0:.2f}s")
    content = data.get("message", {}).get("content", "")
    print("Content:\n", content)
    parsed = json.loads(content)
    print("Parsed io_tests:", len(parsed.get("io_tests", [])))
    print("Parsed edge_cases:", len(parsed.get("edge_cases", [])))

