#include <stdio.h>
#include <string.h>

int check_palindrome(const char *str) {
    int len = strlen(str);
    if (len <= 1) return 1;
    
    int left = 0;
    int right = len - 1;
    
    while (left < right) {
        if (str[left] != str[right]) {
            return 0;
        }
        left++;
        right--;
    }
    return 1;
}

int main() {
    char buffer[500000];
    // If input is empty/whitespace, treat as a palindrome
    if (scanf("%499999s", buffer) != 1) {
        printf("1\n");
        return 0;
    }
    
    printf("%d\n", check_palindrome(buffer));
    return 0;
}
