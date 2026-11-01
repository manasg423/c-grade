#include <stdio.h>
#include <stdlib.h>

void swap(int *a, int *b) {
    int temp = *a;
    *a = *b;
    *b = temp;
}

void reverse(int *nums, int left, int right) {
    while (left < right) {
        swap(&nums[left], &nums[right]);
        left++;
        right--;
    }
}

void rotate(int* nums, int numsSize, int k) {
    if (numsSize <= 1) return;
    k = k % numsSize;
    reverse(nums, 0, numsSize - 1);
    reverse(nums, 0, k - 1);
    reverse(nums, k, numsSize - 1);
}

int main() {
    int size, k;
    if (scanf("%d %d", &size, &k) != 2 || size <= 0) return 0;
    
    int *data = (int*)malloc(size * sizeof(int));
    for (int i = 0; i < size; i++) {
        scanf("%d", &data[i]);
    }

    rotate(data, size, k);

    for (int i = 0; i < size; i++) {
        printf("%d ", data[i]);
    }
    printf("\n");
    
    free(data);
    return 0;
}