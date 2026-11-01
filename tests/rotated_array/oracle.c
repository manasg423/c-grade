#include <stdio.h>
#include <stdlib.h>

void reverse_arr(int *arr, int start, int end) {
    while (start < end) {
        int temp = arr[start];
        arr[start] = arr[end];
        arr[end] = temp;
        start++;
        end--;
    }
}

void rotate_array(int *arr, int n, int k) {
    if (n <= 1) return;
    
    // Normalize K to handle cases where K >= N
    k = k % n;
    if (k < 0) k += n; 
    if (k == 0) return;

    reverse_arr(arr, 0, n - 1);
    reverse_arr(arr, 0, k - 1);
    reverse_arr(arr, k, n - 1);
}

int main() {
    int n, k;
    if (scanf("%d %d", &n, &k) != 2 || n <= 0) {
        return 0;
    }

    int *arr = (int*)malloc(n * sizeof(int));
    if (!arr) return 1;

    for (int i = 0; i < n; i++) {
        scanf("%d", &arr[i]);
    }

    rotate_array(arr, n, k);

    for (int i = 0; i < n; i++) {
        printf("%d%c", arr[i], (i == n - 1) ? '\n' : ' ');
    }

    free(arr);
    return 0;
}