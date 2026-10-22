#include <stdio.h>

long long add(long long a, long long b) {
    return a + b;
}

int main() {
    long long a, b;
    if (scanf("%lld %lld", &a, &b) == 2) {
        printf("%lld\n", add(a, b));
    }
    return 0;
}