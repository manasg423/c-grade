#include <stdio.h>
#include <stdlib.h>

// Helper function for qsort
int compare_ints(const void *a, const void *b) {
    return (*(int*)a - *(int*)b);
}

int main() {
    int n;
    // Handle EOF or invalid format
    if (scanf("%d", &n) != 1) {
        return 0;
    }
    // Handle edge cases: 0 or negative array length
    if (n <= 0) {
        return 0;
    }

    int *arr = (int*)malloc(n * sizeof(int));
    if (!arr) return 1;

    for (int i = 0; i < n; i++) {
        scanf("%d", &arr[i]);
    }

    // Sort the array in O(N log N)
    qsort(arr, n, sizeof(int), compare_ints);

    // Print space-separated, ending with a newline
    for (int i = 0; i < n; i++) {
        printf("%d%c", arr[i], (i == n - 1) ? '\n' : ' ');
    }

    free(arr);
    return 0;
}