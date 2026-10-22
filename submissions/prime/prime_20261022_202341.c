#include <stdio.h>

int check_prime(int num) {
    if (num <= 1) {
        return 0;
    }
    for (int i = 2; i * i <= num; i++) {
        if (num % i == 0) {
            return 0;
        }
    }
    return 1;
}

int main() {
    int number;
    if (scanf("%d", &number) == 1) {
        printf("%d\n", check_prime(number));
    }
    return 0;
}