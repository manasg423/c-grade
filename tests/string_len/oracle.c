#include <stdio.h>
#include <string.h>

int main() {
    char s[256];
    if (scanf("%255s", s) == 1) {
        printf("%zu\n", strlen(s));
    }
    return 0;
}

