#include <stdio.h>
#include <stdlib.h>

typedef struct Edge {
    int to;
    struct Edge* next;
} Edge;

int dfs_color(int u, int c, int* colors, Edge** graph) {
    colors[u] = c;
    Edge* curr = graph[u];
    while (curr) {
        if (colors[curr->to] == c) return 0;
        if (colors[curr->to] == 0 && !dfs_color(curr->to, -c, colors, graph)) {
            return 0;
        }
        curr = curr->next;
    }
    return 1;
}

int main() {
    int v, e;
    if (scanf("%d %d", &v, &e) != 2 || v <= 0) {
        printf("0\n");
        return 0;
    }

    Edge** graph = calloc(v, sizeof(Edge*));
    for (int i = 0; i < e; i++) {
        int from, to;
        scanf("%d %d", &from, &to);
        if (from >= 0 && from < v && to >= 0 && to < v) {
            Edge* e1 = malloc(sizeof(Edge));
            e1->to = to; e1->next = graph[from]; graph[from] = e1;
            Edge* e2 = malloc(sizeof(Edge));
            e2->to = from; e2->next = graph[to]; graph[to] = e2;
        }
    }

    int* colors = calloc(v, sizeof(int));
    int possible = 1;
    for (int i = 0; i < v; i++) {
        if (colors[i] == 0) {
            if (!dfs_color(i, 1, colors, graph)) {
                possible = 0;
                break;
            }
        }
    }

    printf("%d\n", possible);
    return 0;
}
