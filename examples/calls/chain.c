int leaf(int n) {
    int i = 0;
    while (i < n) {
        i++;
    }
    return i;
}

int middle(int n) {
    return leaf(n);
}

int main(void) {
    return middle(3);
}
