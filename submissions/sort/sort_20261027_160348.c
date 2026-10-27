#include <stdio.h>
#include <stdlib.h>

int cmp(const void *a, const void *b) {
    return (*(int*)a - *(int*)b);
}

int main() {
    int size;
    if (scanf("%d", &size) != 1 || size <= 0) return 0;

    int *data = (int*)malloc(size * sizeof(int));
    for (int i = 0; i < size; i++) {
        scanf("%d", &data[i]);
    }

    qsort(data, size, sizeof(int), cmp);

    for (int i = 0; i < size; i++) {
        printf("%d%c", data[i], (i == size - 1) ? '\n' : ' ');
    }

    free(data);
    return 0;
}
