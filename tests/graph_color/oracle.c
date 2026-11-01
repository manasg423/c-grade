#include <stdio.h>
#include <stdlib.h>

typedef struct Node {
    int dest;
    struct Node* next;
} Node;

int is_bipartite(int v, Node** adj) {
    if (v <= 0) return 0;
    int* color = (int*)calloc(v, sizeof(int)); // 0: uncolored, 1: red, -1: blue
    
    for (int i = 0; i < v; i++) {
        if (color[i] == 0) {
            color[i] = 1;
            int* q = (int*)malloc(v * sizeof(int));
            int front = 0, rear = 0;
            q[rear++] = i;
            
            while (front < rear) {
                int curr = q[front++];
                Node* temp = adj[curr];
                while (temp) {
                    if (color[temp->dest] == 0) {
                        color[temp->dest] = -color[curr];
                        q[rear++] = temp->dest;
                    } else if (color[temp->dest] == color[curr]) {
                        free(q);
                        free(color);
                        return 0; // Conflict found
                    }
                    temp = temp->next;
                }
            }
            free(q);
        }
    }
    free(color);
    return 1;
}

int main() {
    int v, e;
    if (scanf("%d %d", &v, &e) != 2 || v <= 0) {
        printf("0\n");
        return 0;
    }

    Node** adj = (Node**)calloc(v, sizeof(Node*));
    
    for (int i = 0; i < e; i++) {
        int u, dest;
        if (scanf("%d %d", &u, &dest) == 2) {
            // Guard against bad generated random inputs
            if (u >= 0 && u < v && dest >= 0 && dest < v) {
                Node* n1 = (Node*)malloc(sizeof(Node));
                n1->dest = dest; n1->next = adj[u]; adj[u] = n1;
                
                Node* n2 = (Node*)malloc(sizeof(Node));
                n2->dest = u; n2->next = adj[dest]; adj[dest] = n2;
            }
        }
    }

    printf("%d\n", is_bipartite(v, adj));

    // Cleanup memory
    for (int i = 0; i < v; i++) {
        Node* temp = adj[i];
        while (temp) {
            Node* next = temp->next;
            free(temp);
            temp = next;
        }
    }
    free(adj);
    return 0;
}