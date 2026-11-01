#include <stdio.h>
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
        printf("-1\n");
        return 0;
    }
    
    int *arr = (int*)malloc(n * sizeof(int));
    if (!arr) return 1;

    for (int i = 0; i < n; i++) {
        scanf("%d", &arr[i]);
    }

    printf("%d\n", find_majority(arr, n));

    free(arr);
    return 0;
}
