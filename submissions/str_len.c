#include <stdio.h>
#include <string.h>

int main() {
    char str[100];
    if (scanf("%99s", str) == 1) {
        printf("%zu\n", strlen(str));
    }
    return 0;
}
