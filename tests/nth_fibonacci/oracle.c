#include <stdio.h>

#define MOD 1000000007

long long compute_fib(int n) {
    if (n <= 0) return 0;
    if (n == 1) return 1;
    long long a = 0, b = 1, c;
    for (int i = 2; i <= n; i++) {
        c = (a + b) % MOD;
        a = b;
        b = c;
    }
    return b;
}

int main() {
    int n;
    if (scanf("%d", &n) != 1) {
        printf("0\n");
        return 0;
    }
    printf("%lld\n", compute_fib(n));
    return 0;
}