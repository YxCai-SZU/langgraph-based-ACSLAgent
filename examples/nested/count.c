int count(int n) {
    int i = 0;
    while (i < n) {
        int j = 0;
        while (j < i) {
            j++;
        }
        i++;
    }
    return i;
}

int main(void) {
    int r = count(3);
    //@ assert r == 3;
    return 0;
}
