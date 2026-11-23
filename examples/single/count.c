int count(int n) {
    int i = 0;
    while (i < n) {
        i++;
    }
    return i;
}

int main(void) {
    int r = count(3);
    //@ assert r == 3;
    return 0;
}
