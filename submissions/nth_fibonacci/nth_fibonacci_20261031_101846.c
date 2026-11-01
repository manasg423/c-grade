#include <stdio.h>

long long get_fib(int n) {
    if (n <= 0) return 0;
    long long prev = 0, curr = 1;
    for (int i = 2; i <= n; i++) {
        long long next = (prev + curr) % 1000000007;
        prev = curr;
        curr = next;
    }
    return curr;
}

int main() {
    int val;
    if (scanf("%d", &val) == 1) {
        printf("%lld\n", get_fib(val));
    } else {
        printf("0\n");
    }
    return 0;
}
