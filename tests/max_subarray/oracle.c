#include <stdio.h>
#include <stdlib.h>

long long find_max_subarray(int *arr, int n) {
    if (n <= 0) return 0;
    long long max_so_far = arr[0];
    long long curr_max = arr[0];
    
    for (int i = 1; i < n; i++) {
        curr_max = (arr[i] > curr_max + arr[i]) ? arr[i] : curr_max + arr[i];
        if (curr_max > max_so_far) {
            max_so_far = curr_max;
        }
    }
    return max_so_far;
}

int main() {
    int n;
    if (scanf("%d", &n) != 1) return 0;
    
    // Edge case handling
    if (n <= 0) {
        printf("0\n");
        return 0;
    }

    int *arr = (int*)malloc(n * sizeof(int));
    if (!arr) return 1;

    for (int i = 0; i < n; i++) {
        scanf("%d", &arr[i]);
    }

    printf("%lld\n", find_max_subarray(arr, n));

    free(arr);
    return 0;
}