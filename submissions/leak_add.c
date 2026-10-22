#include <stdio.h>
#include <stdlib.h>

int main() {
    int *nums = (int *)malloc(2 * sizeof(int));
    if (scanf("%d %d", &nums[0], &nums[1]) == 2) {
        printf("%d\n", nums[0] + nums[1]);
    }
    // Deliberately omitting free(nums) to cause a leak
    return 0;
}