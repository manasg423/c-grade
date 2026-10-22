#include <stdio.h>

int main() {
    int n;
    if (scanf("%d", &n) != 1) {
        return 0;
    }
    if (n <= 1) {
        printf("0\n");
        return 0;
    }
    for (int i = 2; i * i <= n; i++) {
        if (n % i == 0) {
            printf("0\n");
            return 0;
        }
    }
    printf("1\n");
    return 0;
}