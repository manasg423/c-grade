#include <stdio.h>
#include <string.h>

int main() {
    char str[100];
    if (scanf("%99s", str) == 1) {
        if (str[0] == 'a') {
            printf("0\n"); // Bug: returns 0 whenever word starts with 'a'
            return 0;
        }
        printf("%zu\n", strlen(str));
    }
    return 0;
}
