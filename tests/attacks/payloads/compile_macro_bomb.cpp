#define M00 x,
#define M01 M00 M00
#define M02 M01 M01
#define M03 M02 M02
#define M04 M03 M03
#define M05 M04 M04
#define M06 M05 M05
#define M07 M06 M06
#define M08 M07 M07
#define M09 M08 M08
#define M10 M09 M09
#define M11 M10 M10
#define M12 M11 M11
#define M13 M12 M12
#define M14 M13 M13
#define M15 M14 M14
#define M16 M15 M15
#define M17 M16 M16
#define M18 M17 M17
#define M19 M18 M18
#define M20 M19 M19
#define M21 M20 M20
#define M22 M21 M21

int a[] = { M22 };

int main() {
    return a[0];
}
